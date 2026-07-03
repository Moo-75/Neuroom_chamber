
import maze
import time
import json
from datetime import datetime
import multiprocessing
from multiprocessing import freeze_support
import random
import threading
import sys
import cv2
import csv
import os
import math
import pygame

# # maze에서 사용한 library를 다 써야하는게 아닌가? 
# import pandas as pd
# RPi.GPIO as GPIO
# import os
# import pygame

class Task:
    def __init__(self, json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouseid, session, shared_data, dict_lock, start_time, peltier_queue, stop_event): # file_trialdata
        with open (json_dir, "r") as config: #import data
            self.data = json.load(config)
        self.json_dir = json_dir
        self.count_limit = self.data["trial"] 
        self.screen = maze.Display(json_dir) 
        self.sensor = maze.Sensor(json_dir)
        self.reward = maze.Reward(json_dir) 
        self.FP = maze.Photometry(json_dir)
        self.file_trialdata = TrialData_file_name # file format can be decided in the task code.
        self.mouseid = mouseid #
        self.trainingstep = session.split("_")[1] #
        self.day = int(session.split("_")[0].split("d")[1]) #
        self.delay = 0.5
        self.shared_data = shared_data
        self.dict_lock = dict_lock
        self.start_time = start_time
        self.peltier_queue = peltier_queue
        self.stop_event = stop_event

        self.cap = None
        self.out = None
        try:
            print("connecting camera")
            self.cap = cv2.VideoCapture(0)
            self.cap.set(cv2.CAP_PROP_FPS, 10)

            width = int(self.cap.get(3))
            height = int(self.cap.get(4))
            fourcc = cv2.VideoWriter_fourcc(*'DIVX')
            fps = self.cap.get(cv2.CAP_PROP_FPS)  # Set the desired FPS
            self.out = cv2.VideoWriter(Video_file_name, fourcc, fps, (width, height))

            # Create the timestamp file and write the header
            self.timestamp_file = FrameTime_file_name
            os.makedirs(os.path.dirname(self.timestamp_file), exist_ok=True)
            with open(self.timestamp_file, 'w', newline='') as file:
                writer = csv.writer(file)
                writer.writerow(['Frame', 'Timestamp'])
        except:
            print("camera connecting failed")
    def viedeo_record(self, *args):
        if self.cap is None:
            return
        frame_count = 0
        curr_temp = 0.0

        while True:
            ret, frame = self.cap.read()
            if not ret:
                print("Error on reading video")
                break

            if frame_count % 5 == 0:
                with self.dict_lock:
                    curr_temp = self.shared_data["average_temp"]
                if curr_temp is None or not math.isfinite(curr_temp):
                    curr_temp = float("nan")
            
            # Get the current Unix timestamp with decimal places
            unix_timestamp = time.time() - self.start_time
            # Format the Unix timestamp as a string with decimal places
            timestamp_str = f"{unix_timestamp:.3f}"
            # Add the formatted timestamp to the frame
            cv2.putText(frame, timestamp_str, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.putText(frame, f"{curr_temp:.3f}", (1000, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            
            # Write the timestamp and frame count to the CSV file
            with open(self.timestamp_file, 'a', newline='') as file:
                writer = csv.writer(file)
                writer.writerow([frame_count, f"{unix_timestamp:.3f}"])
            
            frame_count += 1
            
            self.out.write(frame)
            if args[0]():
                break
    def task(self):
        pass

    def _temp_status_monitor(self, stop_event, interval_sec=30.0):
        """터미널 한 줄(\\r)에 현재/목표 온도를 주기적으로 갱신."""
        pad = 80
        while True:
            curr_temp, target_temp = self._get_shared_temperatures()
            elapsed_min = (time.time() - self.start_time) / 60.0
            curr_s = f"{curr_temp:.2f}" if curr_temp is not None else "n/a"
            target_s = f"{target_temp:.2f}" if target_temp is not None else "n/a"
            msg = (
                f"[TEMP] t={elapsed_min:.1f}min  "
                f"avg={curr_s}°C  target={target_s}°C"
            )
            sys.stdout.write("\r" + msg.ljust(pad))
            sys.stdout.flush()
            if stop_event.wait(interval_sec):
                break

    def run(self):
        thread_stop = False
        status_stop = threading.Event()
        video_proc = threading.Thread(target=self.viedeo_record, args=(lambda: thread_stop,))
        temp_status_proc = threading.Thread(
            target=self._temp_status_monitor,
            args=(status_stop,),
            daemon=True,
        )
        video_proc.start()
        temp_status_proc.start()
        try:
            self.task()
        except Exception as e:
            print(f"[Task Error] {e}")
        finally:
            status_stop.set()
            temp_status_proc.join(timeout=2.0)
            sys.stdout.write("\n")
            sys.stdout.flush()
            print("done")
            thread_stop = True
            video_proc.join(timeout=5)
            if video_proc.is_alive():
                print("[Warning] video_proc did not terminate cleanly after 5s.")
            if self.cap is not None:
                self.cap.release()
            if self.out is not None:
                self.out.release()

    def _sanitize_temperature(self, value):
        if value is None:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def _get_shared_temperatures(self):
        with self.dict_lock:
            curr_temp = self.shared_data["average_temp"]
            target_temp = self.shared_data["target_temp"]
        return self._sanitize_temperature(curr_temp), self._sanitize_temperature(target_temp)

    def _wait_for_target_temperature(self, target_temp, tolerance, timeout_sec, poll_ms):
        deadline = time.time() + timeout_sec
        last_temp = None
        while time.time() < deadline:
            curr_temp, _ = self._get_shared_temperatures()
            if curr_temp is not None:
                last_temp = curr_temp
                if abs(curr_temp - target_temp) <= tolerance:
                    return True, curr_temp
            pygame.time.wait(poll_ms)
        return False, last_temp

    def _init_ns_attenuation_state(self, switch_min_minutes=14.0, switch_max_minutes=16.0):
        current_state = 'hot' if self.day % 2 == 0 else 'cold'
        attenuation_sign = 1.0 if current_state == 'hot' else -1.0
        min_sec = switch_min_minutes * 60.0
        max_sec = switch_max_minutes * 60.0
        switch_after_sec = random.uniform(min_sec, max_sec)
        switch_state = {
            "next_switch_sec": switch_after_sec,
            "min_sec": min_sec,
            "max_sec": max_sec,
            "switch_count": 0,
        }
        return current_state, attenuation_sign, switch_after_sec, switch_state

    def _maybe_switch_ns_attenuation_state(
        self,
        session_start,
        current_state,
        attenuation_sign,
        switch_after_sec,
        switch_state,
        attenuation_rate,
        attenuation_active,
    ):
        if not isinstance(switch_state, dict):
            switch_state = {
                "next_switch_sec": switch_after_sec,
                "min_sec": 14.0 * 60.0,
                "max_sec": 16.0 * 60.0,
                "switch_count": 0,
            }

        elapsed = time.time() - session_start
        switched_now = False

        while elapsed >= switch_state["next_switch_sec"]:
            current_state = 'cold' if current_state == 'hot' else 'hot'
            attenuation_sign = 1.0 if current_state == 'hot' else -1.0
            switch_state["switch_count"] += 1
            switch_state["next_switch_sec"] += random.uniform(
                switch_state["min_sec"], switch_state["max_sec"]
            )
            switched_now = True
            print(f"NS attenuation state switched to {current_state} at {elapsed:.1f}s")
            if attenuation_active:
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))

        return current_state, attenuation_sign, switch_state, switched_now

    def _ns_cue_for_temperature(self, curr_temp, optimal_ref):
        if curr_temp is None:
            print("[Warning] _ns_cue_for_temperature: curr_temp is None, defaulting to 'hot'/'r'")
            return 'hot', 'r'
        if curr_temp >= optimal_ref:
            return 'cold', 'l'
        return 'hot', 'r'

    def _ns_target_for_side(self, target_temp, poked_side, temp_change, poke_min=23.0, poke_max=37.0):
        if poked_side == 'l':
            if target_temp <= poke_min:
                return target_temp
            return max(target_temp - temp_change, poke_min)
        if target_temp >= poke_max:
            return target_temp
        return min(target_temp + temp_change, poke_max)
    
    # LED + Reward(amount)
    def LED_Reward(self, amount, FP_on=False):
        self.reward.light(True)
        self.reward.give(amount) # Not determined
        print("reward has given")
        RG_time = round(time.time(), 3)
        fp_reward_on = self.FP.FP3_on(on=FP_on)
        if amount > 0:
            rp_flag=0
            while True:
                current_sensor = self.sensor.get()
                if current_sensor[0]==1 and rp_flag==0:
                    RT_time = round(time.time(), 3)
                    fp_reward_off = self.FP.FP3_off(on=FP_on)
                    print("Reward_on")
                    while True:
                        if self.sensor.get()[0] == 0:
                            rp_flag = 1
                            print("Reward_off")
                            break
                if rp_flag==1:
                    break
        else:
            RT_time = round(time.time(), 3)
            fp_reward_off = self.FP.FP3_off(on=FP_on)
        self.reward.light(False)
            
        return RG_time, RT_time, fp_reward_on, fp_reward_off

    def Wrong_LED(self, screen=True):
        wrongLED_on = round(time.time(), 3)
        self.reward.wrong(True)
        if screen:
            self.screen.wrong_screen(side=200, p = (400, 200))

        print("wrong choice")
        while True:
            if time.time() - wrongLED_on >= 5:
                break
        self.reward.wrong(False)
        wrongLED_off = round(time.time(), 3)
        return wrongLED_on, wrongLED_off    

    def sensor2lmr_init(self, choice_tuple, cue_time, timeout=False, FP_on=True):
        current_sensor = self.sensor.get()
        if sum(current_sensor[1:])>=1 and sum(choice_tuple[1:])==0:
            if current_sensor[1] == 1 and choice_tuple[1] == 0:
                choice_tuple[1] = 1
                poke_pos = 'l'
                poke_time = round(time.time(), 3)
                print("l in")
                print(choice_tuple)
                while True:
                    if self.sensor.get()[1] == 0 and choice_tuple[1] == 1:
                        choice_tuple[1] = 0
                        print("l out")
                        break
            elif current_sensor[2] == 1 and choice_tuple[2] == 0:
                self.screen.show() ##
                choice_tuple[2] = 1
                poke_pos = 'm'
                poke_time = round(time.time(), 3)
                print("m in")
                while True:
                    if self.sensor.get()[2] == 0 and choice_tuple[2] == 1:
                        choice_tuple[2] = 0
                        print("m out")
                        break
            elif current_sensor[3] == 1 and choice_tuple[3] == 0:
                choice_tuple[3] = 1
                poke_pos = 'r'
                poke_time = round(time.time(), 3)
                print("r in")
                while True:
                    if self.sensor.get()[3] == 0 and choice_tuple[3] == 1:
                        choice_tuple[3] = 0
                        print("r out")
                        break
                
            return choice_tuple, poke_pos, poke_time
        else:
            return choice_tuple,

    # Initiation
    def Initiation(self, init_delay=True):
        print("Start Initiation")
        self.screen.draw_bar(480, "m", width=266, thickness=None, color=(255, 255, 255), base= 480)
        init_on = round(time.time(), 3)
        print("Initiation on")
        choice_tuple=[0, 0, 0, 0]
        while True:
            poke_result= self.sensor2lmr_init(choice_tuple, init_on, timeout=False)
            if len(poke_result) == 3:
                # self.screen.draw_bar(480, "m", width=266, thickness=None, color=(0,0,0), base= 480)
                if poke_result[1] == "m":
                    init_off=poke_result[2]
                    break
                else:
                    self.screen.draw_bar(480, "m", width=266, thickness=None, color=(255, 255, 255), base= 480)
        # self.screen.show()
        # if init_delay:
        #     while True:
        #         if time.time() - init_off >= self.delay:
        #             break        

        return init_on, init_off

    def Initiation_reward(self, REWARD=True):
        print("Start Initiation")
        self.screen.show(state = ["w"])
        if REWARD == True:
            self.reward.give(0.01)
        init_reward_given = round(time.time(), 3)
        print("Free reward has been given")

        # To protect reward port sensor misworking and confirm the sensor value is changed by mouse
        rp_flag=0
        while True:
            current_sensor = self.sensor.get()
            if current_sensor[0]==1 and rp_flag==0:
                init_reward_taken = round(time.time(), 3)
                print("Reach magazine")
                while True:
                    if self.sensor.get()[0] == 0:
                        rp_flag = 1
                        print("Initiation_off")
                        break
            if rp_flag == 1:
                break
           
        self.screen.show()

        while True:
            if time.time() - init_reward_taken >= 1:
                break

        return init_reward_given, init_reward_taken


    def sensor2lmr(self, choice_tuple, cue_time, timeout=False, loop=True, FP_on=False):
        if loop == True:
            while True:
                if self.sensor.get()[1] == 1 and choice_tuple[1] == 0:
                    self.screen.show()
                    choice_tuple[1] = 1
                    poke_pos = 'l'
                    poke_time = time.time() - self.start_time
                    print("l in")
                    while True:
                        if self.sensor.get()[1] == 0 and choice_tuple[1] == 1:
                            choice_tuple[1] = 0
                            print("l out")
                            break
                    break
                elif self.sensor.get()[3] == 1 and choice_tuple[3] == 0:
                    self.screen.show()
                    choice_tuple[3] = 1
                    poke_pos = 'r'
                    poke_time = time.time() - self.start_time
                    print("r in")
                    while True:
                        if self.sensor.get()[3] == 0 and choice_tuple[3] == 1:
                            choice_tuple[3] = 0
                            print("r out")
                            break
                    break
                elif timeout==True and (time.time() - cue_time) >= 30:
                    poke_pos = "n"
                    print("30 sec passed")
                    poke_time = time.time() - self.start_time
                    break

            return choice_tuple, poke_pos, poke_time
        else:
            current_sensor = self.sensor.get()
            if sum(current_sensor[1:])>=1 and sum(choice_tuple[1:])==0:
                self.screen.show()
                if current_sensor[1] == 1 and choice_tuple[1] == 0:
                    choice_tuple[1] = 1
                    poke_pos = 'l'
                    poke_time = time.time() - self.start_time
                    print("l in")
                    while True:
                        if self.sensor.get()[1] == 0 and choice_tuple[1] == 1:
                            choice_tuple[1] = 0
                            print("l out")
                            break
                elif current_sensor[3] == 1 and choice_tuple[3] == 0:
                    choice_tuple[3] = 1
                    poke_pos = 'r'
                    poke_time = time.time() - self.start_time
                    print("r in")
                    while True:
                        if self.sensor.get()[3] == 0 and choice_tuple[3] == 1:
                            choice_tuple[3] = 0
                            print("r out")
                            break
                   
                return choice_tuple, poke_pos, poke_time
            else:
                return choice_tuple,

    def TrialData2CSV2(self, directory, filename, row, col):
        '''
        This function write csv by adding new row if it exists. If not, than create new directory and file
        Args:
            directory
            filename
            row
            col
        '''

        if not os.path.isdir(directory):
            os.makedirs(directory)
        
        if not os.path.exists(filename):
            with open(filename, "w") as outfile:
                writer = csv.writer(outfile)
                writer.writerow(col)
        
        with open(filename, "a") as outfile:
            writer = csv.writer(outfile)
            writer.writerow(row)

    def Temperature_test(self, stay_time_start=5, task_time=180, stay_time_end=0, start_temp = 10, max_temp = 40, min_temp = 10, d_temp = 4, ITI_duration = 30, FP_on=True,
                         poke_temp = 4.0, optimal_temp = 30.0, hold_seconds=60, reach_tolerance=0.5):
        
        file_name_td = self.file_trialdata+"_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        self.peltier_queue.put(("SET_ATTENUATION", 0))

        col_name_td = ['mouseID', 'Day', 'Task', 'Step', 'Time', 'Event', 'Current_Temp', 'Target_Temp']
        step_index = 0
        safety_timeout = task_time * 60
        poll_interval_ms = 200
        self.screen.show()

        ascending_targets = list(range(int(start_temp), int(max_temp), int(d_temp)))
        ascending_targets.append(int(max_temp))
        descending_targets = list(range(int(max_temp - d_temp), int(min_temp), -int(d_temp)))
        descending_targets.append(int(min_temp))
        target_sequence = ascending_targets + descending_targets

        def get_current_temperature():
            with self.dict_lock:
                curr_temp = self.shared_data.get("average_temp")
                target_temp = self.shared_data.get("target_temp")
            return curr_temp, target_temp

        def log_event(event_name, current_temp, target_temp):
            row = [
                self.mouseid,
                self.day,
                self.trainingstep,
                step_index,
                round(time.time() - self.start_time, 3),
                event_name,
                current_temp if current_temp is not None else 'n',
                target_temp if target_temp is not None else 'n',
            ]
            self.TrialData2CSV2(directory, file_name_td, row, col_name_td)

        def timed_out():
            return (time.time() - self.start_time) >= safety_timeout

        def finish_session(reason):
            current_temp, target_temp = get_current_temperature()
            log_event(reason, current_temp, target_temp)
            self.screen.show(state=["g"])
            self.stop_event.set()

        log_event("SessionStart", *get_current_temperature())

        for target_temp in target_sequence:
            if self.stop_event.is_set():
                finish_session("SessionStopped")
                return

            if timed_out():
                print("Safety timeout reached before completing temperature test.")
                finish_session("SafetyTimeout")
                return

            step_index += 1
            self.peltier_queue.put(("SET_TEMP", float(target_temp)))
            print(f"[Temperature Test] Step {step_index}: target {target_temp:.1f}C")
            current_temp, _ = get_current_temperature()
            log_event("TargetSet", current_temp, target_temp)

            reached = False
            while not self.stop_event.is_set():
                if timed_out():
                    print("Safety timeout reached while waiting for target temperature.")
                    finish_session("SafetyTimeout")
                    return

                current_temp, _ = get_current_temperature()
                if current_temp is not None and abs(current_temp - target_temp) <= reach_tolerance:
                    log_event("TargetReached", current_temp, target_temp)
                    reached = True
                    break

                pygame.time.wait(poll_interval_ms)

            if not reached:
                finish_session("SessionStopped")
                return

            hold_start = time.time()
            current_temp, _ = get_current_temperature()
            log_event("HoldStart", current_temp, target_temp)
            hold_stable_since = time.time()

            while not self.stop_event.is_set():
                if timed_out():
                    print("Safety timeout reached during hold period.")
                    finish_session("SafetyTimeout")
                    return

                current_temp, _ = get_current_temperature()
                if current_temp is None:
                    pygame.time.wait(poll_interval_ms)
                    continue

                if abs(current_temp - target_temp) <= reach_tolerance:
                    if time.time() - hold_stable_since >= hold_seconds:
                        log_event("HoldEnd", current_temp, target_temp)
                        break
                else:
                    hold_stable_since = time.time()

                pygame.time.wait(poll_interval_ms)

        finish_session("SessionEnd")
        return
    
    def Cold_to_hot_block(self, stay_time_start=5, task_time=60, stay_time_end=0, start_temp = 10, max_temp = 25, min_temp = 10, d_temp = 5, ITI_duration = 30, FP_on=True,
                         poke_temp = 4.0, optimal_temp = 30.0):
        
        file_name_td = self.file_trialdata+"_trial-wise.csv"
        directory = os.path.dirname(file_name_td)

        poke_temp = 0

        with self.dict_lock:
            curr_temp = self.shared_data["average_temp"]
            target_temp = self.shared_data["target_temp"]

        # attenuation_factor = - 0.039
        attenuation_factor = 0
        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
        
        col_name_td = ['mouseID', 'Day','Task', 'Trial', "Time", "Event", "Poke_pos", "Photometry_signal"]
        
        choice_tuple=[0,0,0,0]
        i = 0
        target_temp = start_temp

        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "Start", 'n', 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        poke_pos = 'n'


        # process
        while True:
            i += 1
            # Update current_temp (not possible yet)
            
            # self.screen.display_temp_both()
            
            if curr_temp is not None \
                and curr_temp <= optimal_temp - 0.5 or curr_temp > optimal_temp + 0.5:
                self.screen.display_temp_cue("hot")

                cue_on = time.time() - self.start_time
                dt_row = [self.mouseid, self.day, self.trainingstep, i, cue_on, "HotCue", 'n', 'n']
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            choice_tuple=[0,0,0,0]
            
            while True:
                poke_result = self.sensor2lmr(choice_tuple, cue_on, loop=False)
                choice_tuple = poke_result[0]                    
                
                if len(poke_result) == 3:
                    poke_pos = poke_result[1]
                    poke_time = poke_result[2]
                    
                    if poke_pos == "l" or poke_pos == "r":
                        dt_row = [self.mouseid, self.day, self.trainingstep, i, poke_time, "PokeTime", poke_pos,'n'] 
                        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                        break
                
                # Time limit
                if (time.time() - self.start_time) >= task_time*60:
                    dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "End", 'n','n'] 
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                    print("Session time reached maximum. \n Terminating task")
                    self.screen.show(state = ["g"]) #turn gray
                    poke_pos = 'n'
                    break
                
            if poke_pos == 'l':
                print("chose H, TempUP")
                fp_poke_on = self.FP.FP1_on(on=FP_on)
                fp_poke_off = self.FP.FP1_off(on=FP_on)
                # self.screen.display_temp_cue("cold")
                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "TempUp", poke_pos,'n']

                with self.dict_lock:
                    curr_temp = self.shared_data["average_temp"]
                    target_temp = self.shared_data["target_temp"]

                if target_temp + poke_temp >= optimal_temp:
                    self.peltier_queue.put(("TEMP_UPDOWN", optimal_temp - target_temp))
                    block_temp = optimal_temp
                else:
                    self.peltier_queue.put(("TEMP_UPDOWN", poke_temp))
                    block_temp = target_temp + poke_temp
                    self.reward.give(1)

                self.peltier_queue.put(("SET_ATTENUATION", 0))

                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockStart", 'n','n']

                while True:
                    with self.dict_lock:
                        curr_temp = self.shared_data["average_temp"]

                    if curr_temp is not None \
                        and curr_temp >= block_temp - 0.5 and curr_temp < block_temp + 0.5:
                        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
                        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockEnd", 'n','n']
                        break

                    pygame.time.wait(200)

            # if poke_pos == 'r':
            #     print("chose C")
            #     fp_poke_on = self.FP.FP1_on(on=FP_on)
            #     fp_poke_off = self.FP.FP1_off(on=FP_on) 
            #     # self.screen.display_temp_cue("cold")
            #     dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "TempDown", poke_pos,'n'] 
            #     self.peltier_queue.put(("TEMP_UPDOWN", poke_temp))

            # Time limit
            if (time.time() - self.start_time) >= task_time *60:
                self.screen.show(state = ["g"]) #turn gray
                print("Session time reached maximum. \n Terminating task")
                self.stop_event.set()
                break

        return
    
    def Hot_to_cold_block(self, stay_time_start=5, task_time=60, stay_time_end=0, start_temp = 40, max_temp = 25, min_temp = 10, d_temp = 5, ITI_duration = 30, FP_on=True,
                         poke_temp = - 4.0, optimal_temp = 20.0):
        
        file_name_td = self.file_trialdata+"_trial-wise.csv"
        directory = os.path.dirname(file_name_td)

        poke_temp = 0

        with self.dict_lock:
            curr_temp = self.shared_data["average_temp"]
            target_temp = self.shared_data["target_temp"]

        # attenuation_factor = 0.039
        attenuation_factor = 0
        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
        
        col_name_td = ['mouseID', 'Day','Task', 'Trial', "Time", "Event", "Poke_pos", "Photometry_signal"]
        
        choice_tuple=[0,0,0,0]
        i = 0
        target_temp = start_temp

        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "Start", 'n', 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        poke_pos = 'n'

        # process
        while True:
            i += 1
            # Update current_temp (not possible yet)
            
            # self.screen.display_temp_both()
            if curr_temp is not None \
                and curr_temp <= optimal_temp - 0.5 or curr_temp > optimal_temp + 0.5:
                self.screen.display_temp_cue("cold")
                cue_on = time.time() - self.start_time
                dt_row = [self.mouseid, self.day, self.trainingstep, i, cue_on, "ColdCue", 'n', 'n']
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            choice_tuple=[0,0,0,0]
            
            while True:
                poke_result = self.sensor2lmr(choice_tuple, cue_on, loop=False)
                choice_tuple = poke_result[0]                    
                
                if len(poke_result) == 3:
                    poke_pos = poke_result[1]
                    poke_time = poke_result[2]
                    
                    if poke_pos == "l" or poke_pos == "r":
                        dt_row = [self.mouseid, self.day, self.trainingstep, i, poke_time, "PokeTime", poke_pos,'n'] 
                        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                        break
                
                # Time limit
                if (time.time() - self.start_time) >= task_time*60:
                    dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "End", 'n','n'] 
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                    print("Session time reached maximum. \n Terminating task")
                    self.screen.show(state = ["g"]) #turn gray
                    poke_pos = 'n'
                    break
                
            if poke_pos == 'r':
                print("chose C, TempDown")
                fp_poke_on = self.FP.FP1_on(on=FP_on)
                fp_poke_off = self.FP.FP1_off(on=FP_on)
                # self.screen.display_temp_cue("cold")
                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "TempDown", poke_pos,'n']

                with self.dict_lock:
                    curr_temp = self.shared_data["average_temp"]
                    target_temp = self.shared_data["target_temp"]

                if target_temp + poke_temp <= optimal_temp:
                    self.peltier_queue.put(("TEMP_UPDOWN", optimal_temp - target_temp))
                    block_temp = optimal_temp
                else:
                    self.peltier_queue.put(("TEMP_UPDOWN", poke_temp))
                    block_temp = target_temp + poke_temp
                    self.reward.give(1)

                self.peltier_queue.put(("SET_ATTENUATION", 0))

                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockStart", 'n','n']

                while True:
                    with self.dict_lock:
                        curr_temp = self.shared_data["average_temp"]

                    if curr_temp is not None \
                        and curr_temp >= block_temp - 0.5 and curr_temp < block_temp + 0.5:
                        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
                        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockEnd", 'n','n']
                        break

                    pygame.time.wait(200)

            # Time limit
            if (time.time() - self.start_time) >= task_time *60:
                self.screen.show(state = ["g"]) #turn gray
                print("Session time reached maximum. \n Terminating task")
                self.stop_event.set()
                break

        return
    
    def Preference_test(self, stay_time_start=5, task_time=5, stay_time_end=0, start_temp = 40, max_temp = 25, min_temp = 10, d_temp = 5, ITI_duration = 30, FP_on=True,
                         poke_temp = - 4.0, optimal_temp = 25.0):
        
        file_name_td = self.file_trialdata+"_trial-wise.csv"
        directory = os.path.dirname(file_name_td)

        with self.dict_lock:
            curr_temp = self.shared_data["average_temp"]
            target_temp = self.shared_data["target_temp"]

        # attenuation_factor = 0.039
        attenuation_factor = 0
        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
        
        col_name_td = ['mouseID', 'Day','Task', 'Trial', "Time", "Event", "Poke_pos", "Photometry_signal"]
        
        choice_tuple=[0,0,0,0]
        i = 0
        target_temp = start_temp

        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "Start", 'n', 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        poke_pos = 'n'

        # process
        while True:
            i += 1
            # Update current_temp (not possible yet)
            
            # self.screen.display_temp_both()
            if curr_temp is not None \
                and curr_temp <= optimal_temp - 0.5 or curr_temp > optimal_temp + 0.5:
                self.screen.display_temp_both()
                cue_on = time.time() - self.start_time
                dt_row = [self.mouseid, self.day, self.trainingstep, i, cue_on, "BothCue", 'n', 'n']
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            choice_tuple=[0,0,0,0]

            poke_counter = {
                'l': 0,
                'r': 0,
                'n': 0,
            }

            while True:
                self.screen.display_temp_both()
                poke_result = self.sensor2lmr(choice_tuple, cue_on, loop=False)
                choice_tuple = poke_result[0]

                if len(poke_result) == 3:
                    poke_pos = poke_result[1]
                    poke_time = poke_result[2]
                    
                    if poke_pos == "n": continue
                
                    if poke_pos == "l" or poke_pos == "r":
                        dt_row = [self.mouseid, self.day, self.trainingstep, i, poke_time, "PokeTime", poke_pos,'n'] 
                        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                    
                    poke_counter[poke_pos] += 1
                    if poke_counter[poke_pos] > 1:
                        break
                    print(poke_counter)
                
                # Time limit
                if (time.time() - self.start_time) >= 5 * 60:
                    dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "End", 'n','n'] 
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                    print("Session time reached maximum. \n Terminating task")
                    self.screen.show(state = ["g"]) #turn gray
                    poke_pos = 'n'
                    break
            
            if poke_pos == 'l':
                print("chose H, TempUP")
                fp_poke_on = self.FP.FP1_on(on=FP_on)
                fp_poke_off = self.FP.FP1_off(on=FP_on)
                # self.screen.display_temp_cue("cold")
                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "TempUp", poke_pos,'n']

                with self.dict_lock:
                    curr_temp = self.shared_data["average_temp"]
                    target_temp = self.shared_data["target_temp"]

                if target_temp + abs(poke_temp) >= max_temp:
                    self.peltier_queue.put(("TEMP_UPDOWN", max_temp - target_temp))
                    block_temp = max_temp
                else:
                    self.peltier_queue.put(("TEMP_UPDOWN", abs(poke_temp)))
                    block_temp = target_temp + abs(poke_temp)

                self.peltier_queue.put(("SET_ATTENUATION", 0))

                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockStart", 'n','n']

                while True:
                    with self.dict_lock:
                        curr_temp = self.shared_data["average_temp"]

                    if curr_temp is not None \
                        and curr_temp >= block_temp - 0.5 and curr_temp < block_temp + 0.5:
                        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
                        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockEnd", 'n','n']
                        break

                    pygame.time.wait(200)

            if poke_pos == 'r':
                print("chose C, TempDown")
                fp_poke_on = self.FP.FP1_on(on=FP_on)
                fp_poke_off = self.FP.FP1_off(on=FP_on)
                # self.screen.display_temp_cue("cold")
                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "TempDown", poke_pos,'n']

                with self.dict_lock:
                    curr_temp = self.shared_data["average_temp"]
                    target_temp = self.shared_data["target_temp"]

                if target_temp + poke_temp <= min_temp:
                    self.peltier_queue.put(("TEMP_UPDOWN", min_temp - target_temp))
                    block_temp = min_temp
                else:
                    self.peltier_queue.put(("TEMP_UPDOWN", poke_temp))
                    block_temp = target_temp + poke_temp

                self.peltier_queue.put(("SET_ATTENUATION", 0))

                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockStart", 'n','n']

                while True:
                    with self.dict_lock:
                        curr_temp = self.shared_data["average_temp"]

                    if curr_temp is not None \
                        and curr_temp >= block_temp - 0.5 and curr_temp < block_temp + 0.5:
                        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
                        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockEnd", 'n','n']
                        break

                    pygame.time.wait(200)

            # Time limit
            if (time.time() - self.start_time) >= 5 * 60:
                self.screen.show(state = ["g"]) #turn gray
                print("Session time reached maximum. \n Terminating task")
                self.stop_event.set()
                break

        return

    def Find_optimal_block(self, stay_time_start=5, task_time=60, stay_time_end=0, start_temp = 30, max_temp = 40, min_temp = 10, d_temp = 5, ITI_duration = 30, FP_on=True,
                         poke_temp = - 5.0, optimal_temp = 30.0):
        file_name_td = self.file_trialdata+"_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        # attenuation_factor = 0.039
        attenuation_factor = 0
        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
        
        col_name_td = ['mouseID', 'Day','Task', 'Trial', "Time", "Event", "Poke_pos", "Photometry_signal"]
        
        choice_tuple=[0,0,0,0]
        i = 0
        target_temp = start_temp

        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "Start", 'n', 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        poke_pos = 'n'

        # process
        while True:
            i += 1
            # Update current_temp (not possible yet)
            
            self.screen.display_temp_both()
            # self.screen.display_temp_cue("cold")
            cue_on = time.time() - self.start_time
            # if  min_temp < target_temp < max_temp:
            #     self.screen.display_temp_both()
            # elif target_temp <= min_temp:
            #     self.screen.display_temp_cue("hot")
            # elif target_temp >= max_temp:
            #     self.screen.display_temp_cue("cold")
            dt_row = [self.mouseid, self.day, self.trainingstep, i, cue_on, "BothCue", 'n', 'n']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            choice_tuple=[0,0,0,0]
            
            while True:
                poke_result = self.sensor2lmr(choice_tuple, cue_on, loop=False)
                choice_tuple = poke_result[0]                    
                
                if len(poke_result) == 3:
                    poke_pos = poke_result[1]
                    poke_time = poke_result[2]
                    
                    if poke_pos == "l" or poke_pos == "r":
                        dt_row = [self.mouseid, self.day, self.trainingstep, i, poke_time, "PokeTime", poke_pos,'n'] 
                        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                        break
                
                # Time limit
                if (time.time() - self.start_time) >= task_time*60:
                    dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "End", 'n','n'] 
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                    print("Session time reached maximum. \n Terminating task")
                    self.screen.show(state = ["g"]) #turn gray
                    poke_pos = 'n'
                    break
            
            if poke_pos == 'l':
                print("chose H, TempUP")
                fp_poke_on = self.FP.FP1_on(on=FP_on)
                fp_poke_off = self.FP.FP1_off(on=FP_on)
                # self.screen.display_temp_cue("cold")
                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "TempUp", poke_pos,'n']

                with self.dict_lock:
                    curr_temp = self.shared_data["average_temp"]
                    target_temp = self.shared_data["target_temp"]

                if target_temp + abs(poke_temp) >= max_temp:
                    self.peltier_queue.put(("TEMP_UPDOWN", max_temp - target_temp))
                    block_temp = max_temp
                elif target_temp + abs(poke_temp) < max_temp and target_temp + abs(poke_temp) >= optimal_temp:
                    self.peltier_queue.put(("TEMP_UPDOWN", abs(poke_temp)))
                    block_temp = target_temp + abs(poke_temp)
                    attenuation_factor = abs(attenuation_factor)
                    self.reward.give(1)
                else:
                    self.peltier_queue.put(("TEMP_UPDOWN", abs(poke_temp)))
                    block_temp = target_temp + abs(poke_temp)
                    self.reward.give(1)

                self.peltier_queue.put(("SET_ATTENUATION", 0))

                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockStart", 'n','n']

                while True:
                    with self.dict_lock:
                        curr_temp = self.shared_data["average_temp"]

                    if curr_temp is not None \
                        and curr_temp >= block_temp - 0.5 and curr_temp < block_temp + 0.5:
                        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
                        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockEnd", 'n','n']
                        break

                    pygame.time.wait(200)
                
            if poke_pos == 'r':
                print("chose C, TempDown")
                fp_poke_on = self.FP.FP1_on(on=FP_on)
                fp_poke_off = self.FP.FP1_off(on=FP_on)
                # self.screen.display_temp_cue("cold")
                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "TempDown", poke_pos,'n']

                with self.dict_lock:
                    curr_temp = self.shared_data["average_temp"]
                    target_temp = self.shared_data["target_temp"]

                if target_temp - abs(poke_temp) <= min_temp:
                    self.peltier_queue.put(("TEMP_UPDOWN", min_temp - target_temp))
                    block_temp = min_temp
                elif target_temp - abs(poke_temp) > min_temp and target_temp - abs(poke_temp) <= optimal_temp:
                    self.peltier_queue.put(("TEMP_UPDOWN", - abs(poke_temp)))
                    block_temp = target_temp - abs(poke_temp)
                    attenuation_factor = - abs(attenuation_factor)
                    self.reward.give(1)
                else:
                    block_temp = target_temp - abs(poke_temp)
                    self.peltier_queue.put(("TEMP_UPDOWN", - abs(poke_temp)))
                    self.reward.give(1)

                self.peltier_queue.put(("SET_ATTENUATION", 0))

                dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockStart", 'n','n']

                while True:
                    with self.dict_lock:
                        curr_temp = self.shared_data["average_temp"]

                    if curr_temp is not None \
                        and curr_temp >= block_temp - 0.5 and curr_temp < block_temp + 0.5:
                        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
                        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "BlockEnd", 'n','n']
                        break

                    pygame.time.wait(200)

            # Time limit
            if (time.time() - self.start_time) >= task_time *60:
                self.screen.show(state = ["g"]) #turn gray
                print("Session time reached maximum. \n Terminating task")
                self.stop_event.set()
                break

        return
    
    def POA_task(self, stay_time_start=5, task_time=60, stay_time_end=0, start_temp = 30, max_temp = 40, min_temp = 10, d_temp = 5, ITI_duration = 30, FP_on=True,
                         poke_temp = - 5.0, optimal_temp = 30.0):
        file_name_td = self.file_trialdata+"_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        # attenuation_factor = 0.039
        attenuation_factor = 0
        self.peltier_queue.put(("SET_ATTENUATION", attenuation_factor))
        
        col_name_td = ['mouseID', 'Day','Task', 'Trial', "Time", "Event", "Poke_pos", "Photometry_signal"]
        
        choice_tuple=[0,0,0,0]
        i = 0
        target_temp = start_temp

        dt_row = [self.mouseid, self.day, self.trainingstep, i, time.time() - self.start_time, "Start", 'n', 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        poke_pos = 'n'

        start_time = time.time()
        
        # Define phase parameters (duration in seconds, target temperature, phase name)
        phases = [
            ("Baseline1", self.data["poa_task_vars"]["baseline1_duration"] * 60, self.data["poa_task_vars"]["baseline1_temperature"]),
            ("Moulation1", self.data["poa_task_vars"]["moulation1_duration"] * 60, self.data["poa_task_vars"]["moulation1_temperature"]),
            ("Baseline2", self.data["poa_task_vars"]["baseline2_duration"] * 60, self.data["poa_task_vars"]["baseline2_temperature"]),
            ("Moulation2", self.data["poa_task_vars"]["moulation2_duration"] * 60, self.data["poa_task_vars"]["moulation2_temperature"]),
            ("Baseline3", self.data["poa_task_vars"]["baseline3_duration"] * 60, self.data["poa_task_vars"]["baseline3_temperature"]),
        ]

        phase_start_time = start_time

        for phase_name, phase_duration, phase_temp in phases:
            phase_end_time = phase_start_time + phase_duration
            self.peltier_queue.put(("SET_TEMP", phase_temp))
            last_report_minute = None
            last_report_seconds = None
            while time.time() < phase_end_time:
                now = time.time()
                elapsed_in_phase = now - phase_start_time
                remaining_in_phase = phase_end_time - now

                # Every 60 seconds (or at entry)
                # To avoid missing the moment if sleep is too long,
                # we track the last reported minute.
                current_minute = int(elapsed_in_phase // 60)
                current_seconds = int(elapsed_in_phase % 60)
                if (last_report_minute is None) or (current_minute > last_report_minute) or (current_seconds == last_report_seconds + 30):
                    with self.dict_lock:
                        curr_temp = self.shared_data.get("average_temp", None)
                    print(
                        f"[{phase_name}] Elapsed: {elapsed_in_phase:.0f}s "
                        f"(min {current_minute}), "
                        f"Remaining: {remaining_in_phase:.0f}s, "
                        f"Current Temp: {curr_temp if curr_temp is not None else 'N/A'}"
                    )
                    last_report_minute = current_minute
                    last_report_seconds = current_seconds
                pygame.time.wait(1000)  # To reduce CPU usage

            phase_start_time = phase_end_time

        print("POA task completed. \n Terminating task")
        self.stop_event.set()

        return
    
    
    # ============================================================
    # Temperature Training Protocol - Stage 1, 2, 3
    # ============================================================
    
    def Training_Stage1(self, start_temp=23.0, task_time=30, max_trials=100,
                        temp_change=3.0, choice_window=30, temp_hold_time=10, iti_duration=10,
                        optimal_min=27.0, optimal_max=33.0, choice_min=23.0, choice_max=37.0,
                        optimal_threshold=30.0, post_reach_duration=10):
        """
        Stage 1: Poke-Temperature Association
        - 목표: Nose poke가 온도 변화를 유발한다는 것을 학습
        - Attenuation: OFF
        - State: 없음
        - 종료 조건: Optimal 온도(30도) 도달 후 10분 경과 시 종료
        """
        print("=== Training Stage 1: Poke-Temperature Association ===")
        
        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event', 
                       'Current_Temp', 'Target_Temp', 'Poke_pos', 'Response_Time']
        
        
        # Load parameters from JSON if available
        if "TS_params" in self.data and "Stage1" in self.data["TS_params"]:
            params = self.data["TS_params"]["Stage1"]
            task_time = params.get("task_time", task_time)
            choice_window = params.get("choice_window", choice_window)
            temp_hold_time = params.get("temp_hold_time", temp_hold_time)
            print(f"Loaded Stage 1 params from JSON: task_time={task_time}, choice_window={choice_window}, temp_hold_time={temp_hold_time}")

        # 초기 설정
        target_temp = start_temp
        self.peltier_queue.put(("SET_TEMP", target_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))  # Attenuation OFF
        
        optimal_reached_time = None
        is_start_cold = start_temp < optimal_threshold
        
        trial = 0
        start_Ex = time.time()
        
        # 초기 온도 도달 대기
        print(f"Waiting for initial temperature: {target_temp}°C")
        while True:
            current_state, attenuation_sign, state_switched, _ = self._maybe_switch_ns_attenuation_state(
                start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                state_switched, attenuation_rate, attenuation_active
            )
            curr_temp, _ = self._get_shared_temperatures()
            if curr_temp is not None and abs(curr_temp - target_temp) < 1.0:
                break
            if time.time() - start_Ex > 300:
                print(f"Initial temperature did not reach {target_temp} C. Last temperature: {curr_temp}")
                return
            pygame.time.wait(500)
        
        dt_row = [self.mouseid, self.day, self.trainingstep, trial, 
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', 0]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        
        # Trial 루프
        while trial < max_trials and (time.time() - start_Ex) < task_time * 60:
            trial += 1
            print(f"\n--- Trial {trial} ---")
            
            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp
            
            # Optimal 도달 체크 및 종료 타이머 확인
            if optimal_reached_time is None:
                if is_start_cold and curr_temp >= optimal_threshold:
                    optimal_reached_time = time.time()
                    print(f"!!! Optimal temperature ({optimal_threshold}°C) reached! Session will end in {post_reach_duration} min. !!!")
                elif not is_start_cold and curr_temp <= optimal_threshold:
                    optimal_reached_time = time.time()
                    print(f"!!! Optimal temperature ({optimal_threshold}°C) reached! Session will end in {post_reach_duration} min. !!!")
            
            if optimal_reached_time is not None:
                passed_time = time.time() - optimal_reached_time
                print(f"Time since optimal reached: {passed_time/60:.1f} min / {post_reach_duration} min")
                if passed_time > (post_reach_duration * 60):
                    print(f"--- {post_reach_duration} minutes passed after reaching optimal. Ending session. ---")
                    break
            
            # 1. Trial Cue (소리만, wait 없음)
            self.reward.give(0.1)  # 밸브 소리 cue
            self.reward.give(0.1)  # 밸브 소리 cue
            cue_time = time.time() - self.start_time
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      cue_time, "TrialCue", curr_temp, target_temp, 'n', 0]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
            
            # 2. Choice Window - 양쪽 cue 표시
            self.screen.display_temp_both()
            choice_start = time.time()
            choice_tuple = [0, 0, 0, 0]
            poke_pos = 'n'
            poke_time = 0
            
            while (time.time() - choice_start) < choice_window:
                poke_result = self.sensor2lmr(choice_tuple, choice_start, loop=False)
                choice_tuple = poke_result[0]
                
                if len(poke_result) == 3:
                    poke_pos = poke_result[1]
                    poke_time = poke_result[2]
                    response_time = time.time() - choice_start
                    break
                    
                pygame.time.wait(5)
            
            # 3. Choice 처리 + Outcome Phase
            if poke_pos == 'l':  # Left = Hot
                # Choice: LEFT (Hot)
                print(f"Choice: LEFT (Hot) +{temp_change}°C")
                if target_temp >= choice_max:
                    new_target = target_temp
                else:
                    new_target = min(target_temp + temp_change, choice_max)
                self.peltier_queue.put(("SET_TEMP", new_target))
                
                # Valid Choice Sound Cue
                if new_target != target_temp:
                    self.reward.give(0.1)
                
                # Outcome: 온도 상승 방향 표시
                self.screen.show(state=["warm"])
                
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_L_Hot", curr_temp, new_target, 'l', response_time]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                
                # 온도 변화 시간
                pygame.time.wait(int(temp_hold_time * 1000))
                
            elif poke_pos == 'r':  # Right = Cool
                # Choice: RIGHT (Cool)
                print(f"Choice: RIGHT (Cool) -{temp_change}°C")
                if target_temp <= choice_min:
                    new_target = target_temp
                else:
                    new_target = max(target_temp - temp_change, choice_min)
                self.peltier_queue.put(("SET_TEMP", new_target))
                
                # Valid Choice Sound Cue
                if new_target != target_temp:
                    self.reward.give(0.1)
                
                # Outcome: 온도 하강 방향 표시
                self.screen.show(state=["cool"])
                
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_R_Cool", curr_temp, new_target, 'r', response_time]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                
                # 온도 변화 시간
                pygame.time.wait(int(temp_hold_time * 1000))
                
            else:  # No choice
                print("No Choice - Temperature maintained")
                
                # Outcome: 화면 OFF (no choice feedback)
                self.screen.show()
                
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "NoChoice", curr_temp, target_temp, 'n', 0]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                
                # 온도 유지 시간
                pygame.time.wait(int(temp_hold_time * 1000))
            
            # 4. ITI - 화면 OFF
            self.screen.show()
            pygame.time.wait(int(iti_duration * 1000))
            
            with self.dict_lock:
                curr_temp = self.shared_data["average_temp"]
            
            # Optimal 도달 체크 (여기서도 한 번 더 로그 찍어줌)
            if optimal_min <= curr_temp <= optimal_max:
                print(f"Current temp {curr_temp:.1f}°C is in optimal range!")
        
        # 세션 종료
        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionEnd", curr_temp, target_temp, 'n', 0]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        
        print(f"\n=== Stage 1 Complete: {trial} trials ===")
        self.screen.show(state=["g"])
        self.stop_event.set()
        return
    
    
    def Training_Stage2(self, start_temp=25.0, task_time=35, max_trials=100,
                        temp_change=3.0, choice_window=30, temp_hold_time=10, iti_duration=0,
                        attenuation_rate=0.02, optimal_min=27.0, optimal_max=33.0,
                        choice_min=10.0, choice_max=40.0, state_change_prob=0.7):
        """
        Stage 2: Attenuation + State Introduction
        - 목표: Attenuation과 State 개념 학습
        - Attenuation: ON (0.02°C/초)
        - State: 있음 (화면 cue 제공)
        - Attenuation은 no choice 발생 시점부터 다음 choice까지 지속 적용
        """
        print("=== Training Stage 2: Attenuation + State ===")
        
        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Poke_pos', 'Response_Time', 'State', 'Attenuation_Active']
        
        
        # Load parameters from JSON if available
        if "TS_params" in self.data and "Stage2" in self.data["TS_params"]:
            params = self.data["TS_params"]["Stage2"]
            task_time = params.get("task_time", task_time)
            choice_window = params.get("choice_window", choice_window)
            temp_hold_time = params.get("temp_hold_time", temp_hold_time)
            print(f"Loaded Stage 2 params from JSON: task_time={task_time}, choice_window={choice_window}, temp_hold_time={temp_hold_time}")

        # 초기 설정
        target_temp = start_temp
        self.peltier_queue.put(("SET_TEMP", target_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))  # 초기에는 attenuation OFF
        
        # State: 'hot' = 온도 상승 방향, 'cold' = 온도 하강 방향
        # 2026-02-10 Modify: Start state based on start_temp
        if target_temp < optimal_min:
            current_state = 'cold'
        elif target_temp > optimal_max:
            current_state = 'hot'
        else:
            current_state = random.choice(['hot', 'cold'])
        attenuation_active = False  # no choice 발생 시 True, choice 발생 시 False
        
        trial = 0
        start_Ex = time.time()
        
        # 초기 온도 도달 대기
        print(f"Waiting for initial temperature: {target_temp}°C")
        reached_target, curr_temp = self._wait_for_target_temperature(
            target_temp, tolerance=1.0, timeout_sec=300, poll_ms=500
        )
        if not reached_target:
            print(f"Initial temperature did not reach {target_temp} C. Last temperature: {curr_temp}")
            return
        
        # State cue 표시 (Stage 2에서는 세션 시작 시 State 표시)
        self.screen.display_temp_cue(current_state)
        print(f"Initial State: {current_state}")
        
        # was_outside_optimal 초기화 (실제 온도 기반)
        was_outside_optimal = curr_temp < optimal_min or curr_temp > optimal_max
        
        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        
        # Trial 루프
        while trial < max_trials and (time.time() - start_Ex) < task_time * 60:
            trial += 1
            print(f"\n--- Trial {trial} (State: {current_state}, Attenuation: {attenuation_active}) ---")
            
            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp
            
            # Optimal 밖에 있는지 체크 (다음 trial을 위해 업데이트는 trial 끝에서 수행)
            
            # 1. Trial Cue (소리만, wait 없음)
            self.reward.give(0.1)
            self.reward.give(0.1)
            
            cue_time = time.time() - self.start_time
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      cue_time, "TrialCue", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
            
            # 2. Choice Window - 양쪽 cue 표시
            self.screen.display_temp_both()
            choice_start = time.time()
            choice_tuple = [0, 0, 0, 0]
            poke_pos = 'n'
            response_time = 0
            
            while (time.time() - choice_start) < choice_window:
                poke_result = self.sensor2lmr(choice_tuple, choice_start, loop=False)
                choice_tuple = poke_result[0]
                
                if len(poke_result) == 3:
                    poke_pos = poke_result[1]
                    response_time = time.time() - choice_start
                    break
                    
                pygame.time.wait(5)
            with self.dict_lock:
                prev_temp = self.shared_data["target_temp"]

            # 3. Choice 처리 + Outcome Phase
            if poke_pos == 'l':  # Left = Hot
                # Choice: LEFT (Hot)
                print(f"Choice: LEFT (Hot) +{temp_change}°C")
                if target_temp >= choice_max:
                    new_target = target_temp
                else:
                    new_target = min(target_temp + temp_change, choice_max)
                self.peltier_queue.put(("SET_TEMP", new_target))
                
                # Valid Choice Sound Cue
                if new_target != target_temp:
                    self.reward.give(0.1)
                
                # Choice 발생 → Attenuation OFF
                attenuation_active = False
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))
                
                # Outcome: 온도 상승 방향 표시
                self.screen.show(state=["warm"])
                
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_L_Hot", curr_temp, new_target, 'l', response_time, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                
                # State 전환 체크: optimal 밖 → optimal 안으로 진입
                if was_outside_optimal and optimal_min <= new_target <= optimal_max:
                    if random.random() < state_change_prob:
                        current_state = 'cold' if prev_temp > optimal_max else 'hot'
                    else:
                        current_state = random.choice(['hot', 'cold'])
                    print(f"State changed to: {current_state}")
                
                pygame.time.wait(int(temp_hold_time * 1000))
                
            elif poke_pos == 'r':  # Right = Cool
                # Choice: RIGHT (Cool)
                print(f"Choice: RIGHT (Cool) -{temp_change}°C")
                if target_temp <= choice_min:
                    new_target = target_temp
                else:
                    new_target = max(target_temp - temp_change, choice_min)
                self.peltier_queue.put(("SET_TEMP", new_target))
                
                # Valid Choice Sound Cue
                if new_target != target_temp:
                    self.reward.give(0.1)
                
                # Choice 발생 → Attenuation OFF
                attenuation_active = False
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))
                
                # Outcome: 온도 하강 방향 표시
                self.screen.show(state=["cool"])
                
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_R_Cool", curr_temp, new_target, 'r', response_time, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                
                # State 전환 체크
                if was_outside_optimal and optimal_min <= new_target <= optimal_max:
                    if random.random() < state_change_prob:
                        current_state = 'hot' if prev_temp < optimal_min else 'cold'
                    else:
                        current_state = random.choice(['hot', 'cold'])
                    print(f"State changed to: {current_state}")
                
                pygame.time.wait(int(temp_hold_time * 1000))
                
            else:  # No choice
                print("No Choice - Attenuation activated")
                
                # No choice → Attenuation ON
                attenuation_active = True
                attenuation_direction = attenuation_rate if current_state == 'hot' else -attenuation_rate
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_direction))
                
                # Outcome: Attenuation 방향 표시
                self.screen.show()
                
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "NoChoice", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                
                pygame.time.wait(int(temp_hold_time * 1000))
            
            # 4. ITI - 화면 OFF (Attenuation은 계속 유지)
            self.screen.show()
            pygame.time.wait(int(iti_duration * 1000))
            
            # optimal 밖 상태 업데이트
            with self.dict_lock:
                curr_temp = self.shared_data["average_temp"]
            was_outside_optimal = curr_temp < optimal_min or curr_temp > optimal_max
        
        # 세션 종료
        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionEnd", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        
        print(f"\n=== Stage 2 Complete: {trial} trials ===")
        self.screen.show(state=["g"])
        self.stop_event.set()
        return
    
    
    def Training_Stage3(self, start_temp=30.0, task_time=40, max_trials=100,
                        temp_change=3.0, choice_window=15, temp_hold_time=10, iti_duration=0,
                        attenuation_rate=0.03, optimal_min=27.0, optimal_max=33.0,
                        choice_min=10.0, choice_max=40.0, state_change_prob=0.7):
        """
        Stage 3: Full Task - No State Cue
        - 목표: State cue 없이 Full task 수행
        - Attenuation: ON (0.03°C/초)
        - State: 있음 (cue 없음 - 마우스가 추론)
        """
        print("=== Training Stage 3: Full Task (No State Cue) ===")
        
        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Poke_pos', 'Response_Time', 'State', 'Attenuation_Active']
        
        
        # Load parameters from JSON if available
        if "TS_params" in self.data and "Stage3" in self.data["TS_params"]:
            params = self.data["TS_params"]["Stage3"]
            task_time = params.get("task_time", task_time)
            choice_window = params.get("choice_window", choice_window)
            temp_hold_time = params.get("temp_hold_time", temp_hold_time)
            print(f"Loaded Stage 3 params from JSON: task_time={task_time}, choice_window={choice_window}, temp_hold_time={temp_hold_time}")

        # 초기 설정
        target_temp = start_temp
        self.peltier_queue.put(("SET_TEMP", target_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))
        
        # 2026-02-16 Modify: Alternating start state based on day
        if self.day % 2 == 0:
            current_state = 'hot'  # Even day -> Hot start
        else:
            current_state = 'cold' # Odd day -> Cold start
        
        # current_state = random.choice(['hot', 'cold'])
        attenuation_active = False
        
        trial = 0
        start_Ex = time.time()
        
        # 초기 온도 도달 대기
        print(f"Waiting for initial temperature: {target_temp}°C")
        while True:
            with self.dict_lock:
                curr_temp = self.shared_data["average_temp"]
            if curr_temp is not None and abs(curr_temp - target_temp) < 1.0:
                break
            pygame.time.wait(500)
        
        # Stage 3: State cue 표시 안 함
        self.screen.show()
        print(f"Initial State (hidden): {current_state}")
        
        # was_outside_optimal 초기화 (실제 온도 기반)
        was_outside_optimal = curr_temp < optimal_min or curr_temp > optimal_max
        
        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        
        # Trial 루프
        while trial < max_trials and (time.time() - start_Ex) < task_time * 60:
            trial += 1
            print(f"\n--- Trial {trial} (State: {current_state} [hidden], Attenuation: {attenuation_active}) ---")
            
            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp
            
            currently_outside_optimal = curr_temp < optimal_min or curr_temp > optimal_max
            
            # 1. Trial Cue (소리만, wait 없음) - Stage 3: State cue 없음
            self.reward.give(0.1)
            self.reward.give(0.1)
            
            cue_time = time.time() - self.start_time
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      cue_time, "TrialCue", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
            
            # 2. Choice Window - 양쪽 cue 표시
            self.screen.display_temp_both()
            choice_start = time.time()
            choice_tuple = [0, 0, 0, 0]
            poke_pos = 'n'
            response_time = 0
            
            while (time.time() - choice_start) < choice_window:
                poke_result = self.sensor2lmr(choice_tuple, choice_start, loop=False)
                choice_tuple = poke_result[0]
                
                if len(poke_result) == 3:
                    poke_pos = poke_result[1]
                    response_time = time.time() - choice_start
                    break
                    
                pygame.time.wait(5)
            
            # 3. Choice 처리 + Outcome Phase
            if poke_pos == 'l':  # Left = Hot
                # Choice: LEFT (Hot)
                print(f"Choice: LEFT (Hot) +{temp_change}°C")
                if target_temp >= choice_max:
                    new_target = target_temp
                else:
                    new_target = min(target_temp + temp_change, choice_max)
                self.peltier_queue.put(("SET_TEMP", new_target))
                
                # Valid Choice Sound Cue
                if new_target != target_temp:
                    self.reward.give(0.1)
                
                attenuation_active = False
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))
                
                # Outcome: 온도 상승 방향 표시
                self.screen.show(state=["warm"])
                
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_L_Hot", curr_temp, new_target, 'l', response_time, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                
                # State 전환 체크
                if was_outside_optimal and optimal_min <= new_target <= optimal_max:
                    if random.random() < state_change_prob:
                        current_state = 'cold' if prev_temp > optimal_max else 'hot'
                    else:
                        current_state = random.choice(['hot', 'cold'])
                    print(f"State changed to (hidden): {current_state}")
                
                pygame.time.wait(int(temp_hold_time * 1000))
                
            elif poke_pos == 'r':  # Right = Cool
                # Choice: RIGHT (Cool)
                print(f"Choice: RIGHT (Cool) -{temp_change}°C")
                if target_temp <= choice_min:
                    new_target = target_temp
                else:
                    new_target = max(target_temp - temp_change, choice_min)
                self.peltier_queue.put(("SET_TEMP", new_target))
                
                # Valid Choice Sound Cue
                if new_target != target_temp:
                    self.reward.give(0.1)
                
                attenuation_active = False
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))
                
                # Outcome: 온도 하강 방향 표시
                self.screen.show(state=["cool"])
                
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_R_Cool", curr_temp, new_target, 'r', response_time, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                
                # State 전환 체크
                if was_outside_optimal and optimal_min <= new_target <= optimal_max:
                    if random.random() < state_change_prob:
                        current_state = 'hot' if prev_temp < optimal_min else 'cold'
                    else:
                        current_state = random.choice(['hot', 'cold'])
                    print(f"State changed to (hidden): {current_state}")
                
                pygame.time.wait(int(temp_hold_time * 1000))
                
            else:  # No choice
                print("No Choice - Attenuation activated")
                
                attenuation_active = True
                attenuation_direction = attenuation_rate if current_state == 'hot' else -attenuation_rate
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_direction))
                
                # Outcome: Attenuation 방향 표시 (Stage 3에서는 마우스가 추론해야 하지만 시각적 피드백 제공)
                self.screen.show()
                
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "NoChoice", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                
                pygame.time.wait(int(temp_hold_time * 1000))
            
            # 4. ITI - 화면 OFF
            self.screen.show()
            pygame.time.wait(int(iti_duration * 1000))
            
            with self.dict_lock:
                curr_temp = self.shared_data["average_temp"]
            was_outside_optimal = curr_temp < optimal_min or curr_temp > optimal_max
        
        # 세션 종료
        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionEnd", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        
        print(f"\n=== Stage 3 Complete: {trial} trials ===")
        self.screen.show(state=["g"])
        self.stop_event.set()
        return

    # ============================================================
    # New Protocol Stages (NS1 ~ NS4)
    # ============================================================

    def New_Stage1(self, start_temp=30.0, task_time=60,
                   attenuation_rate=0.07, temp_change=4.0, temp_tolerance=0.5,
                   optimal_ref=30.0, att_min=15.0, att_max=45.0):
        """
        New Protocol Stage 1: Center Poke -> Temperature Change
        - Center에 현재 온도 기반으로 항상 정답 cue를 표시
        - 마우스가 center를 poke하면 즉시 온도 변화
        - Attenuation 있음 (0.02도/초)
        """
        print("=== New Protocol Stage 1: Center Poke -> Temp Change ===")

        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Poke_count', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Cue_Type', 'State']

        target_temp = start_temp
        self.peltier_queue.put(("SET_TEMP", target_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))  # 대기 중 일단 OFF

        # Day 기준 state 결정 (홀수=cold start, 짝수=hot start)
        current_state, attenuation_sign, state_switch_after_sec, state_switched = self._init_ns_attenuation_state()

        poke_count = 0
        start_Ex = time.time()
        attenuation_active = False

        # 초기 온도 도달 대기
        print(f"Waiting for initial temperature: {target_temp}°C")
        while True:
            with self.dict_lock:
                curr_temp = self.shared_data["average_temp"]
            if curr_temp is not None and abs(curr_temp - target_temp) < 1.0:
                break
            pygame.time.wait(500)

        # Attenuation 시작
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
        attenuation_active = True

        dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', current_state]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        print(f"State: {current_state}, Attenuation: {attenuation_rate * attenuation_sign:+.4f}°C/s")

        att_resume_time = 0.0  # attenuation 재활성화 예약 시각 (0.0 = 즉시 가능)

        # 메인 루프
        while (time.time() - start_Ex) < task_time * 60:
            if not attenuation_active and time.time() >= att_resume_time:
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                attenuation_active = True
            current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                state_switched, attenuation_rate, attenuation_active
            )
            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp

            if switched_now:
                dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                          time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, 'n', current_state]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            cue_type, _ = self._ns_cue_for_temperature(curr_temp, optimal_ref)

            self.screen.display_temp_cue_center(cue_type)

            cue_time = time.time() - self.start_time
            dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                      cue_time, "CueOn", curr_temp, target_temp, cue_type, current_state]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            print(f"  Cue: {cue_type.upper()} | Temp: {curr_temp:.1f}°C | Waiting center poke...")

            # Center poke 대기
            choice_tuple = [0, 0, 0, 0]
            center_poked = False
            switched_now = False  # inner loop 진입 전 초기화 (outer loop 이중 기록 방지)
            while not center_poked:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                if (time.time() - start_Ex) >= task_time * 60:
                    break
                current_sensor = self.sensor.get()  # [reward, left, center, right]
                curr_temp, target_temp = self._get_shared_temperatures()
                if target_temp is None:
                    target_temp = start_temp
                if curr_temp is None:
                    curr_temp = target_temp
                updated_cue_type, _ = self._ns_cue_for_temperature(curr_temp, optimal_ref)
                if switched_now:
                    dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, 'n', current_state]
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if updated_cue_type != cue_type:
                    cue_type = updated_cue_type
                    self.screen.display_temp_cue_center(cue_type)
                    dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                              time.time() - self.start_time, "CueUpdate", curr_temp, target_temp, cue_type, current_state]
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if current_sensor[2] == 1 and choice_tuple[2] == 0:
                    choice_tuple[2] = 1
                    center_poked = True
                    poke_count += 1
                    poke_time = time.time() - self.start_time
                    # 센서에서 나올 때까지 대기
                    while self.sensor.get()[2] == 1:
                        pygame.time.wait(5)
                    choice_tuple[2] = 0
                if not attenuation_active and time.time() >= att_resume_time:
                    self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                    attenuation_active = True
                pygame.time.wait(5)

            if not center_poked:
                break  # 세션 종료

            print(f"  Center poke #{poke_count}! (Cue was: {cue_type.upper()})")

            # Sound cue
            self.reward.give(0.1)

            # Attenuation OFF (온도 변화 동안 일시 중단)
            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))
            attenuation_active = False

            # 온도 변화 지시
            curr_temp, current_target = self._get_shared_temperatures()
            if curr_temp is None:
                curr_temp = current_target if current_target is not None else target_temp

            cue_side = 'r' if cue_type == 'hot' else 'l'
            new_target = self._ns_target_for_side(target_temp, cue_side, temp_change)

            self.peltier_queue.put(("SET_TEMP", new_target))

            dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                      poke_time, "CenterPoke", curr_temp, new_target, cue_type, current_state]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            print(f"  Temp change: {curr_temp:.1f} -> {new_target:.1f}°C")

            # 흰 화면 (온도 변화 중 대기)
            self.screen.show(state=["w"])

            # 목표온도 도달 대기
            reached_target = False
            wait_start = time.time()
            while True:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                curr_temp, target_temp = self._get_shared_temperatures()
                if switched_now:
                    dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, 'n', current_state]
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if curr_temp is not None and abs(curr_temp - new_target) <= temp_tolerance:
                    reached_target = True
                    break
                if time.time() - wait_start > 60:  # 최대 60초 대기
                    break
                pygame.time.wait(200)

            dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                      time.time() - self.start_time, "TempReached" if reached_target else "TempTimeout", curr_temp, new_target, cue_type, current_state]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            # 온도 도달 후 30초간 attenuation 비활성화 유지 (비블로킹)
            att_resume_time = time.time() + 30
            print(f"Attenuation hold: resumes in 30s")

        # 세션 종료
        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp
        dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                  time.time() - self.start_time, "SessionEnd", curr_temp, target_temp, 'n', current_state]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        print("=== New Stage 1 Session Ended ===")

    def TRL1(self, start_temp=15.0, temp_max=45.0, temp_min=15.0, bump_deg=3.0,
             attenuation_rate=0.07, white_sec=5.0, task_time=60,
             bump_arrival_tolerance=1.0, bump_arrival_timeout_sec=180.0):
        """
        TRL1: Center-only reversal. Attenuation off until first center poke.

        Each poke: (1) 보상 후 흰 화면 ON → 센터 포크 불가(대기 루프 밖),
        (2) drift OFF + 명령 목표 ±bump_deg,
        (3) 실측이 범프 목표 도달 시 흰 화면 OFF → 포크 가능,
        (4) attenuation_rate (°C/s) 드리프트 ON.

        white_sec 인자는 하위 호환용(미사용). 흰 화면 길이는 범프 목표 도달까지.
        Preview cue: hot = next heating bump, cold = next cooling bump.
        """
        print("=== TRL1: Center attenuation reversal ===")

        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Poke_count', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Preview_Cue', 'Atten_Dir']

        self.peltier_queue.put(("SET_TEMP", start_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))

        current_mode = None  # None | 'heating' | 'cooling'
        poke_count = 0
        start_Ex = time.time()

        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp

        dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'hot', 'off']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        session_done = False
        while (time.time() - start_Ex) < task_time * 60 and not session_done:
            next_is_heating = current_mode != 'heating'
            preview = 'hot' if next_is_heating else 'cold'
            self.screen.display_temp_cue_center(preview)

            choice_tuple = [0, 0, 0, 0]
            center_poked = False
            while not center_poked:
                if (time.time() - start_Ex) >= task_time * 60:
                    session_done = True
                    break
                current_sensor = self.sensor.get()
                if current_sensor[2] == 1 and choice_tuple[2] == 0:
                    choice_tuple[2] = 1
                    center_poked = True
                    poke_count += 1
                    poke_time = time.time() - self.start_time
                    while self.sensor.get()[2] == 1:
                        pygame.time.wait(5)
                    choice_tuple[2] = 0
                pygame.time.wait(5)

            if session_done or not center_poked:
                break

            self.reward.give(0.1)

            curr_temp, target_temp = self._get_shared_temperatures()
            if curr_temp is None:
                curr_temp = target_temp if target_temp is not None else start_temp
            # 범프는 반드시 "현재 명령 목표" 기준(드리프트 중 목표가 챔버보다 앞서 있을 때
            # 실측만 쓰면 한 포크에 수십 °C 목표 점프가 날 수 있음 — batch6 TRL1 로그에서 확인됨)
            ref = target_temp
            if ref is None or not math.isfinite(ref):
                ref = curr_temp

            if current_mode is None:
                new_mode = 'heating'
            else:
                new_mode = 'cooling' if current_mode == 'heating' else 'heating'

            if new_mode == 'heating':
                new_target = min(ref + bump_deg, temp_max)
                att_sign = 1.0
            else:
                new_target = max(ref - bump_deg, temp_min)
                att_sign = -1.0

            dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                      poke_time, "CenterPoke", curr_temp, new_target, preview, new_mode]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            print(
                f"[TRL1] Poke #{poke_count} ({new_mode}): "
                f"cmd_ref={ref:.3f}°C, avg_now={curr_temp:.3f}°C → bump_target={new_target:.3f}°C"
            )

            # 범프 시작: 흰 화면 + 이 구간에서는 포크 대기 루프에 없어 연속 포크 불가
            self.screen.show(state=["w"])
            dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                      time.time() - self.start_time, "WhiteOn", curr_temp, new_target, preview, new_mode]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))
            self.peltier_queue.put(("SET_TEMP", new_target))
            ok_bump, last_avg = self._wait_for_target_temperature(
                new_target, bump_arrival_tolerance, bump_arrival_timeout_sec, 100
            )
            if (time.time() - start_Ex) >= task_time * 60:
                session_done = True

            # 범프 목표 도달(또는 타임아웃) 시 흰 화면 종료 → 다시 포크 가능
            self.screen.show()
            curr_temp, target_temp = self._get_shared_temperatures()
            dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                      time.time() - self.start_time, "WhiteOff", curr_temp, target_temp, preview, new_mode]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            if last_avg is not None and math.isfinite(last_avg):
                last_s = f"{last_avg:.3f}"
            else:
                last_s = "n/a"
            print(
                f"[TRL1] Bump arrival: {'OK' if ok_bump else 'TIMEOUT'}, "
                f"last_avg={last_s}°C, bump_target={new_target:.3f}°C "
                f"(tol ±{bump_arrival_tolerance}°C)"
            )
            if not ok_bump:
                print(
                    f"[TRL1] (attenuation will still engage) timeout {bump_arrival_timeout_sec}s"
                )

            self.peltier_queue.put(
                ("SET_ATTENUATION_DIRECT", (attenuation_rate * att_sign, temp_min, temp_max))
            )
            current_mode = new_mode

            if session_done:
                break

        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp
        att_label = current_mode if current_mode else 'off'
        dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                  time.time() - self.start_time, "SessionEnd", curr_temp, target_temp, 'n', att_label]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        print("=== TRL1 Session Ended ===")

    def TRL2(self, start_temp=15.0, temp_max=45.0, temp_min=15.0, bump_deg=3.0,
             attenuation_rate=0.07, task_time=60,
             bump_arrival_tolerance=1.0, bump_arrival_timeout_sec=180.0):
        """
        TRL2: 방향 학습. left = cold choice(감쇠 냉각), right = hot choice(감쇠 가열).
        한 시점에는 한쪽만 큐 제시(hot↔cold 번갈아). 비활성 구멍 poke는 무시.
        올바른 poke → 흰화면 + 감쇠 정지 → ±bump_deg → 흰화면 해제 후
        다음 큐로 전환하고, hot 완료 후에는 +rate 냉각 큐(좌), cold 완료 후에는 -rate 가열 큐(우).
        """
        print("=== TRL2: Side alternating hot/cold cue ===")

        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event',
                         'Current_Temp', 'Target_Temp', 'ActiveCue', 'PokePos', 'AttSign']

        self.peltier_queue.put(("SET_TEMP", start_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))

        # 'hot' = right만 유효, 'cold' = left만 유효. 첫 제시는 hot(우).
        active_cue = 'hot'
        trial = 0
        start_Ex = time.time()

        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp

        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart",
                  curr_temp, target_temp, active_cue, 'n', 'off']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        session_done = False
        while (time.time() - start_Ex) < task_time * 60 and not session_done:
            if active_cue == 'hot':
                self.screen.display_temp_cue('hot')
            else:
                self.screen.display_temp_cue('cold')

            correct = False
            poke_pos = 'n'
            prev_s = (0, 0, 0, 0)
            while not correct:
                if (time.time() - start_Ex) >= task_time * 60:
                    session_done = True
                    break
                s = self.sensor.get()
                if active_cue == 'hot':
                    if s[3] == 1 and prev_s[3] == 0:
                        correct = True
                        poke_pos = 'right'
                        trial += 1
                        poke_t = time.time() - self.start_time
                        while self.sensor.get()[3] == 1:
                            pygame.time.wait(5)
                else:
                    if s[1] == 1 and prev_s[1] == 0:
                        correct = True
                        poke_pos = 'left'
                        trial += 1
                        poke_t = time.time() - self.start_time
                        while self.sensor.get()[1] == 1:
                            pygame.time.wait(5)
                prev_s = s
                pygame.time.wait(5)

            if session_done:
                break

            self.reward.give(0.1)

            curr_temp, target_temp = self._get_shared_temperatures()
            if curr_temp is None:
                curr_temp = target_temp if target_temp is not None else start_temp
            ref = target_temp
            if ref is None or not math.isfinite(ref):
                ref = curr_temp

            if active_cue == 'hot':
                new_target = min(ref + bump_deg, temp_max)
                att_sign_after = 1.0
            else:
                new_target = max(ref - bump_deg, temp_min)
                att_sign_after = -1.0

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      poke_t, "CorrectPoke", curr_temp, new_target, active_cue, poke_pos,
                      '+' if att_sign_after > 0 else '-']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            print(
                f"[TRL2] trial {trial} ({active_cue}): ref={ref:.3f}°C → bump_target={new_target:.3f}°C, "
                f"next drift={'+' if att_sign_after > 0 else '-'}{attenuation_rate}°C/s"
            )

            self.screen.show(state=["w"])
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "WhiteOn", curr_temp, new_target,
                      active_cue, poke_pos, 'off']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))
            self.peltier_queue.put(("SET_TEMP", new_target))
            ok_bump, last_avg = self._wait_for_target_temperature(
                new_target, bump_arrival_tolerance, bump_arrival_timeout_sec, 100
            )
            if (time.time() - start_Ex) >= task_time * 60:
                session_done = True

            self.screen.show()
            curr_temp, target_temp = self._get_shared_temperatures()
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "WhiteOff", curr_temp, target_temp,
                      active_cue, poke_pos, '+' if att_sign_after > 0 else '-']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.peltier_queue.put(
                ("SET_ATTENUATION_DIRECT", (attenuation_rate * att_sign_after, temp_min, temp_max))
            )

            active_cue = 'cold' if active_cue == 'hot' else 'hot'

            if session_done:
                break

        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp
        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionEnd", curr_temp, target_temp,
                  active_cue, 'n', 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        print("=== TRL2 Session Ended ===")

    def TRL3(self, start_temp=15.0, temp_max=45.0, temp_min=15.0, bump_deg=3.0,
             attenuation_rate=0.07, task_time=60,
             bump_arrival_tolerance=1.0, bump_arrival_timeout_sec=180.0,
             single_cue_probability=0.5, max_consecutive_both=3):
        """
        TRL3: TRL_main과 동일한 범프·감쇠(att) 로직.
        매 trial마다 P(both)=(1-single_cue_probability)로 양쪽 큐(TRL_main),
        P(single)=single_cue_probability로 한쪽만 큐.
        single-cue: trial 시작 시점 att_sign(드리프트 부호, hot=+1 / cold=-1)에 따라
        감쇠 방향의 반대쪽 큐만 제시(att off(0)이면 hot 큐).
        양쪽 큐(both)는 연속 max_consecutive_both회까지만 허용(기본 3 → 네 번째 연속 both는 금지).
        """
        print("=== TRL3: mixed single-cue / both-cue (att off until bump; then directional drift) ===")
        _both_nominal_pct = (1.0 - single_cue_probability) * 100.0
        print(
            f"[TRL3] nominal both-cue ≈ {_both_nominal_pct:.1f}% / single-cue ≈ "
            f"{single_cue_probability * 100.0:.1f}% (연속 both 상한으로 실제 비율은 다를 수 있음)"
        )

        def _trl3_att_label(sign):
            if sign == 0:
                return 'off'
            return '+' if sign > 0 else '-'

        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Choice', 'PokePos', 'AttSign', 'CueLayout']

        self.peltier_queue.put(("SET_TEMP", start_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))

        att_sign = 0.0
        trial = 0
        start_Ex = time.time()

        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp

        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart",
                  curr_temp, target_temp, 'n', 'n', _trl3_att_label(att_sign), 'mixed']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        consecutive_both = 0
        session_done = False
        while (time.time() - start_Ex) < task_time * 60 and not session_done:
            use_both = random.random() >= single_cue_probability
            if use_both and consecutive_both >= max_consecutive_both:
                use_both = False
            cue_layout = 'both' if use_both else 'single'

            choice = None
            poke_pos = 'n'
            prev_s = (0, 0, 0, 0)

            if use_both:
                self.screen.display_temp_both()
                while choice is None:
                    if (time.time() - start_Ex) >= task_time * 60:
                        session_done = True
                        break
                    s = self.sensor.get()
                    if s[3] == 1 and prev_s[3] == 0:
                        choice = 'hot'
                        poke_pos = 'right'
                        trial += 1
                        poke_t = time.time() - self.start_time
                        while self.sensor.get()[3] == 1:
                            pygame.time.wait(5)
                    elif s[1] == 1 and prev_s[1] == 0:
                        choice = 'cold'
                        poke_pos = 'left'
                        trial += 1
                        poke_t = time.time() - self.start_time
                        while self.sensor.get()[1] == 1:
                            pygame.time.wait(5)
                    prev_s = s
                    pygame.time.wait(5)
            else:
                # trial 시작 시점 감쇠(드리프트) 방향과 반대 큐만 제시
                active_cue = 'cold' if att_sign > 0 else 'hot'
                if active_cue == 'hot':
                    self.screen.display_temp_cue('hot')
                else:
                    self.screen.display_temp_cue('cold')

                correct = False
                prev_s = (0, 0, 0, 0)
                while not correct:
                    if (time.time() - start_Ex) >= task_time * 60:
                        session_done = True
                        break
                    s = self.sensor.get()
                    if active_cue == 'hot':
                        if s[3] == 1 and prev_s[3] == 0:
                            correct = True
                            choice = 'hot'
                            poke_pos = 'right'
                            trial += 1
                            poke_t = time.time() - self.start_time
                            while self.sensor.get()[3] == 1:
                                pygame.time.wait(5)
                    else:
                        if s[1] == 1 and prev_s[1] == 0:
                            correct = True
                            choice = 'cold'
                            poke_pos = 'left'
                            trial += 1
                            poke_t = time.time() - self.start_time
                            while self.sensor.get()[1] == 1:
                                pygame.time.wait(5)
                    prev_s = s
                    pygame.time.wait(5)

            if session_done:
                break

            self.reward.give(0.1)

            curr_temp, target_temp = self._get_shared_temperatures()
            if curr_temp is None:
                curr_temp = target_temp if target_temp is not None else start_temp
            ref = target_temp
            if ref is None or not math.isfinite(ref):
                ref = curr_temp

            if choice == 'hot':
                new_target = min(ref + bump_deg, temp_max)
            else:
                new_target = max(ref - bump_deg, temp_min)

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      poke_t, "CorrectPoke", curr_temp, new_target, choice, poke_pos,
                      _trl3_att_label(att_sign), cue_layout]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            print(
                f"[TRL3] trial {trial} layout={cue_layout} choice={choice}, att_sign before={_trl3_att_label(att_sign)}, "
                f"ref={ref:.3f}°C → bump_target={new_target:.3f}°C (clamped to [{temp_min}, {temp_max}])"
            )

            self.screen.show(state=["w"])
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "WhiteOn", curr_temp, new_target,
                      choice, poke_pos, _trl3_att_label(att_sign), cue_layout]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))
            self.peltier_queue.put(("SET_TEMP", new_target))
            ok_bump, last_avg = self._wait_for_target_temperature(
                new_target, bump_arrival_tolerance, bump_arrival_timeout_sec, 100
            )
            if (time.time() - start_Ex) >= task_time * 60:
                session_done = True

            self.screen.show()
            curr_temp, target_temp = self._get_shared_temperatures()

            choice_sign = 1.0 if choice == 'hot' else -1.0
            if not math.isclose(att_sign, choice_sign, rel_tol=0.0, abs_tol=1e-9):
                att_sign = choice_sign

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "WhiteOff", curr_temp, target_temp,
                      choice, poke_pos, _trl3_att_label(att_sign), cue_layout]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            drift_rate = attenuation_rate * att_sign
            self.peltier_queue.put(
                ("SET_ATTENUATION_DIRECT", (drift_rate, temp_min, temp_max))
            )

            if use_both:
                consecutive_both += 1
            else:
                consecutive_both = 0

            if session_done:
                break

        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp
        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionEnd", curr_temp, target_temp,
                  'n', 'n', _trl3_att_label(att_sign), 'mixed']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        print("=== TRL3 Session Ended ===")

    def TRL_main(self, start_temp=15.0, temp_max=45.0, temp_min=15.0, bump_deg=3.0,
                 attenuation_rate=0.07, task_time=60,
                 bump_arrival_tolerance=1.0, bump_arrival_timeout_sec=180.0):
        """
        TRL_main: TRL2와 동일하게 시작 — 감쇠 0, 좌 cold·우 hot 동시 제시(항상).
        범프 목표: ref±bump_deg 후 [temp_min, temp_max]로 클램프(hot 상한·cold 하한 동일 규칙).
        범프 후 감쇠 방향: choice_sign( hot=+1, cold=-1 )와 att_sign이 같으면 유지, 다르면 att_sign=choice_sign.
        """
        print("=== TRL_main: Both cues from start (att off); hot/cold bump + directional att ===")

        def _trl3_att_label(sign):
            if sign == 0:
                return 'off'
            return '+' if sign > 0 else '-'

        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Choice', 'PokePos', 'AttSign', 'CueLayout']

        self.peltier_queue.put(("SET_TEMP", start_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))

        # 0 = 감쇠 끔(TRL2 초기와 동일), +1 = hot drift, -1 = cold drift
        att_sign = 0.0
        trial = 0
        start_Ex = time.time()

        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp

        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart",
                  curr_temp, target_temp, 'both', 'n', _trl3_att_label(att_sign), 'both']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        session_done = False
        while (time.time() - start_Ex) < task_time * 60 and not session_done:
            self.screen.display_temp_both()

            choice = None
            poke_pos = 'n'
            prev_s = (0, 0, 0, 0)
            while choice is None:
                if (time.time() - start_Ex) >= task_time * 60:
                    session_done = True
                    break
                s = self.sensor.get()
                if s[3] == 1 and prev_s[3] == 0:
                    choice = 'hot'
                    poke_pos = 'right'
                    trial += 1
                    poke_t = time.time() - self.start_time
                    while self.sensor.get()[3] == 1:
                        pygame.time.wait(5)
                elif s[1] == 1 and prev_s[1] == 0:
                    choice = 'cold'
                    poke_pos = 'left'
                    trial += 1
                    poke_t = time.time() - self.start_time
                    while self.sensor.get()[1] == 1:
                        pygame.time.wait(5)
                prev_s = s
                pygame.time.wait(5)

            if session_done:
                break

            self.reward.give(0.1)

            curr_temp, target_temp = self._get_shared_temperatures()
            if curr_temp is None:
                curr_temp = target_temp if target_temp is not None else start_temp
            ref = target_temp
            if ref is None or not math.isfinite(ref):
                ref = curr_temp

            if choice == 'hot':
                new_target = min(ref + bump_deg, temp_max)
            else:
                new_target = max(ref - bump_deg, temp_min)

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      poke_t, "CorrectPoke", curr_temp, new_target, choice, poke_pos,
                      _trl3_att_label(att_sign), 'both']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            print(
                f"[TRL_main] trial {trial} choice={choice}, att_sign before={_trl3_att_label(att_sign)}, "
                f"ref={ref:.3f}°C → bump_target={new_target:.3f}°C (clamped to [{temp_min}, {temp_max}])"
            )

            self.screen.show(state=["w"])
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "WhiteOn", curr_temp, new_target,
                      choice, poke_pos, _trl3_att_label(att_sign), 'both']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))
            self.peltier_queue.put(("SET_TEMP", new_target))
            ok_bump, last_avg = self._wait_for_target_temperature(
                new_target, bump_arrival_tolerance, bump_arrival_timeout_sec, 100
            )
            if (time.time() - start_Ex) >= task_time * 60:
                session_done = True

            self.screen.show()
            curr_temp, target_temp = self._get_shared_temperatures()

            choice_sign = 1.0 if choice == 'hot' else -1.0
            if not math.isclose(att_sign, choice_sign, rel_tol=0.0, abs_tol=1e-9):
                att_sign = choice_sign

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "WhiteOff", curr_temp, target_temp,
                      choice, poke_pos, _trl3_att_label(att_sign), 'both']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            drift_rate = attenuation_rate * att_sign
            self.peltier_queue.put(
                ("SET_ATTENUATION_DIRECT", (drift_rate, temp_min, temp_max))
            )

            if session_done:
                break

        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp
        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionEnd", curr_temp, target_temp,
                  'both', 'n', _trl3_att_label(att_sign), 'both']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        print("=== TRL_main Session Ended ===")

    # ============================================================
    # Temperature Lift (TL) - 선행 학습 단계
    # ============================================================

    def _run_temperature_lift(self, bump_choices,
                              start_temp=10.0, temp_min=10.0, temp_max=40.0,
                              no_choice_drop_choices=(2.5, 3.0, 3.5),
                              bump_balance_block=20,
                              no_choice_balance_block=20,
                              choice_window=20.0, feedback_window=40.0,
                              task_time=60, blink_period=1.0,
                              trial_start_reward=0.1, choice_reward=0.1):
        """
        TL 공통 코어. Left-only, trial-based.
        - 한 trial: choice window(기본 20s) + feedback window(기본 40s).
        - Continuous drift is disabled; the setpoint is held during the choice window.
        - choice window 동안 left poke(sensor[1]) 발생 시 즉시 feedback 시작.
          미발생 시 no choice로 기록 후 feedback 시작. left 외 poke는 무시.
        - choice한 경우 feedback window 동안 drift 정지 + (choice 시점 측정 avg + bump)로 SET_TEMP,
          끝까지 유지. no choice면 feedback 시작 때 balanced random drop을 적용.
        - choice 상승폭 bump는 20-trial balanced random bag에서 선택.
        - sound cue = reward 밸브 소리: trial 시작 + left poke 시 reward.give.
        """
        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Choice', 'Bump', 'RT',
                       'OutcomeDelta', 'OutcomeTarget_Temp']

        no_choice_drop_bag = []
        bump_bag = []
        bump_block_index = 0
        no_choice_drop_block_index = 0

        def make_balanced_bag(values, block_size, block_index):
            values = list(values)
            if not values:
                return []
            block_size = max(1, int(block_size))
            base_count = block_size // len(values)
            extra_count = block_size % len(values)
            counts = [base_count] * len(values)
            for i in range(extra_count):
                counts[(block_index + i) % len(values)] += 1
            bag = []
            for value, count in zip(values, counts):
                bag.extend([value] * count)
            random.shuffle(bag)
            return bag

        def next_bump():
            nonlocal bump_bag, bump_block_index
            if not bump_bag:
                bump_bag = make_balanced_bag(
                    bump_choices, bump_balance_block, bump_block_index
                )
                bump_block_index += 1
            return bump_bag.pop()

        def next_no_choice_drop():
            nonlocal no_choice_drop_bag, no_choice_drop_block_index
            if not no_choice_drop_bag:
                no_choice_drop_bag = make_balanced_bag(
                    no_choice_drop_choices,
                    no_choice_balance_block,
                    no_choice_drop_block_index,
                )
                no_choice_drop_block_index += 1
            return no_choice_drop_bag.pop()

        def outcome_base_temp(curr_temp, target_temp):
            if target_temp is not None:
                return target_temp
            if curr_temp is not None:
                return curr_temp
            return start_temp

        shared_start_temp = None
        with self.dict_lock:
            shared_start_temp = self.shared_data.get("initial_target_temp")
        shared_start_temp = self._sanitize_temperature(shared_start_temp)
        if shared_start_temp is not None:
            if shared_start_temp < temp_min or shared_start_temp > temp_max:
                clamped_start = max(temp_min, min(shared_start_temp, temp_max))
                print(
                    f"[TL] initial target {shared_start_temp}°C is outside "
                    f"{temp_min}-{temp_max}°C; clamped to {clamped_start}°C"
                )
                start_temp = clamped_start
            else:
                start_temp = shared_start_temp
            print(f"[TL] using maintemp set-on target as start_temp: {start_temp}°C")

        self.peltier_queue.put(("SET_TEMP", start_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))
        with self.dict_lock:
            self.shared_data["target_temp"] = start_temp

        trial = 0
        start_Ex = time.time()

        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp

        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart",
                  curr_temp, target_temp, 'n', 'n', 'n', 'n', 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        session_done = False
        while (time.time() - start_Ex) < task_time * 60 and not session_done:
            trial += 1
            outcome_delta = 'n'
            outcome_target = 'n'

            # --- Trial 시작: target hold + sound cue(밸브 소리) ---
            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))
            self.reward.give(trial_start_reward)
            curr_temp, target_temp = self._get_shared_temperatures()
            print(f"\n[TL] --- Trial {trial} start (no drift) ---")
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "TrialStart",
                      curr_temp, target_temp, 'n', 'n', 'n', 'n', 'n']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            # --- Choice window: left cue blink + left poke 검출 ---
            cw_start = time.time()
            prev_left = 0
            choice = False
            poke_t = None
            blink_on = None  # None이면 첫 진입 시 강제 ON
            last_blink = cw_start
            while True:
                now = time.time()
                if (now - start_Ex) >= task_time * 60:
                    session_done = True
                    break
                if (now - cw_start) >= choice_window:
                    break

                # cue blink (~blink_period 마다 토글)
                if blink_on is None or (now - last_blink) >= blink_period:
                    blink_on = True if blink_on is None else (not blink_on)
                    last_blink = now
                    if blink_on:
                        self.screen.display_temp_cue("cold")  # 왼쪽 위치 cue (cosmetic)
                    else:
                        self.screen.show()

                s = self.sensor.get()
                if s[1] == 1 and prev_left == 0:
                    choice = True
                    poke_t = now - self.start_time
                    while self.sensor.get()[1] == 1:
                        pygame.time.wait(5)
                    break
                prev_left = s[1]
                pygame.time.wait(5)

            # --- Choice 처리 ---
            if choice:
                self.reward.give(choice_reward)
                curr_temp, target_temp = self._get_shared_temperatures()
                if curr_temp is None:
                    curr_temp = target_temp if target_temp is not None else start_temp
                bump = next_bump()
                base_temp = outcome_base_temp(curr_temp, target_temp)
                new_target = max(temp_min, min(base_temp + bump, temp_max))
                rt = round(poke_t - (cw_start - self.start_time), 3)

                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))
                self.peltier_queue.put(("SET_TEMP", new_target))
                self.screen.show(state=["w"])
                outcome_delta = bump
                outcome_target = new_target

                print(f"[TL] trial {trial}: LeftPoke, target={base_temp:.3f}°C + bump {bump} "
                      f"→ target {new_target:.3f}°C (RT={rt}s)")
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          poke_t, "LeftPoke", curr_temp, new_target, 'l', bump, rt,
                          outcome_delta, outcome_target]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
            else:
                if not session_done:
                    self.screen.show(state=["w"])  # feedback white screen
                    curr_temp, target_temp = self._get_shared_temperatures()
                    if curr_temp is None:
                        curr_temp = target_temp if target_temp is not None else start_temp
                    drop = next_no_choice_drop()
                    base_temp = outcome_base_temp(curr_temp, target_temp)
                    new_target = max(temp_min, min(base_temp - drop, temp_max))
                    self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))
                    self.peltier_queue.put(("SET_TEMP", new_target))
                    outcome_delta = -drop
                    outcome_target = new_target
                    print(f"[TL] trial {trial}: NoChoice, target={base_temp:.3f} - drop {drop} "
                          f"-> target {new_target:.3f}")
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "NoChoice",
                              curr_temp, new_target, 'n', -drop, 'n',
                              outcome_delta, outcome_target]
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            if session_done:
                break

            # --- Feedback window: poke 무시, 고정 타이머 ---
            fb_start = time.time()
            while (time.time() - fb_start) < feedback_window:
                if (time.time() - start_Ex) >= task_time * 60:
                    session_done = True
                    break
                pygame.time.wait(50)

            curr_temp, target_temp = self._get_shared_temperatures()
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "FeedbackEnd",
                      curr_temp, target_temp, 'l' if choice else 'n', 'n', 'n',
                      outcome_delta, outcome_target]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            if session_done:
                break

        self.screen.show(state=["g"])
        curr_temp, target_temp = self._get_shared_temperatures()
        if target_temp is None:
            target_temp = start_temp
        if curr_temp is None:
            curr_temp = target_temp
        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionEnd",
                  curr_temp, target_temp, 'n', 'n', 'n', 'n', 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
        self.stop_event.set()
        print("=== TL Session Ended ===")

    def TL1(self):
        print("=== TL1: Temperature lift (+5/-5 fixed) ===")
        self._run_temperature_lift(
            bump_choices=(5.0,),
            no_choice_drop_choices=(5.0,),
        )

    def TL2(self):
        print("=== TL2: Temperature lift (+3/3.5/4, -1.5/-2/-2.5, 10s/20s) ===")
        self._run_temperature_lift(
            bump_choices=(3.0, 3.5, 4.0),
            no_choice_drop_choices=(1.5, 2.0, 2.5),
            choice_window=10.0,
            feedback_window=20.0,
        )

    def New_Stage2(self, start_temp=30.0, task_time=60,
                   attenuation_rate=0.07, temp_change=4.0, temp_tolerance=0.5,
                   optimal_ref=30.0, att_min=15.0, att_max=45.0, choice_timeout=120):
        """
        New Protocol Stage 2: Center Poke -> Side Cue -> Temp Change
        - Center poke 시 현재 온도에 따른 정답측 side에 cue 제시
        - 오답 side poke는 무시, 120초 timeout 시 cue 제거 후 처음으로
        """
        print("=== New Protocol Stage 2: Center -> Side Cue -> Temp Change ===")

        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Cue_Type', 'Poke_pos', 'State', 'Result']

        target_temp = start_temp
        self.peltier_queue.put(("SET_TEMP", target_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))

        current_state, attenuation_sign, state_switch_after_sec, state_switched = self._init_ns_attenuation_state()

        trial = 0
        start_Ex = time.time()
        attenuation_active = False

        print(f"Waiting for initial temperature: {target_temp}°C")
        while True:
            current_state, attenuation_sign, state_switched, _switched = self._maybe_switch_ns_attenuation_state(
                start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                state_switched, attenuation_rate, attenuation_active
            )
            curr_temp, _ = self._get_shared_temperatures()
            if curr_temp is not None and abs(curr_temp - target_temp) < 1.0:
                break
            if time.time() - start_Ex > 300:
                print(f"Initial temperature did not reach {target_temp} C. Last temperature: {curr_temp}")
                return
            pygame.time.wait(500)

        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
        attenuation_active = True
        self.screen.show()  # 빈 화면

        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', 'n', current_state, 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        print(f"State: {current_state}")

        att_resume_time = 0.0  # attenuation 재활성화 예약 시각 (0.0 = 즉시 가능)

        while (time.time() - start_Ex) < task_time * 60:
            if not attenuation_active and time.time() >= att_resume_time:
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                attenuation_active = True
            current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                state_switched, attenuation_rate, attenuation_active
            )
            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp
            if switched_now:
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, 'n', 'n', current_state, 'state_switch']
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.screen.display_start_cue_center()

            # Center poke 대기
            choice_tuple = [0, 0, 0, 0]
            center_poked = False
            while not center_poked:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                if (time.time() - start_Ex) >= task_time * 60:
                    break
                current_sensor = self.sensor.get()
                if switched_now:
                    curr_temp, target_temp = self._get_shared_temperatures()
                    if target_temp is None:
                        target_temp = start_temp
                    if curr_temp is None:
                        curr_temp = target_temp
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, 'n', 'n', current_state, 'state_switch']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if current_sensor[2] == 1 and choice_tuple[2] == 0:
                    choice_tuple[2] = 1
                    center_poked = True
                    while self.sensor.get()[2] == 1:
                        pygame.time.wait(5)
                    choice_tuple[2] = 0
                if not attenuation_active and time.time() >= att_resume_time:
                    self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                    attenuation_active = True
                pygame.time.wait(5)

            if not center_poked:
                break

            trial += 1
            center_poke_time = time.time() - self.start_time
            self.reward.give(0.1)  # Sound cue

            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp

            cue_type, correct_side = self._ns_cue_for_temperature(curr_temp, optimal_ref)
            self.screen.display_temp_cue(cue_type)

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      center_poke_time, "CenterPoke", curr_temp, target_temp, cue_type, 'm', current_state, 'n']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            print(f"Trial {trial} | Temp: {curr_temp:.1f}°C | Cue: {cue_type.upper()} at {correct_side}")

            # Side poke 대기 (timeout: choice_timeout 초)
            side_choice_start = time.time()
            choice_tuple = [0, 0, 0, 0]
            result = 'timeout'
            poke_pos = 'n'
            poke_time = center_poke_time
            new_target = target_temp

            while (time.time() - side_choice_start) < choice_timeout:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                if (time.time() - start_Ex) >= task_time * 60:
                    result = 'session_end'
                    break
                current_sensor = self.sensor.get()
                curr_temp, target_temp = self._get_shared_temperatures()
                if target_temp is None:
                    target_temp = start_temp
                if curr_temp is None:
                    curr_temp = target_temp
                updated_cue_type, updated_correct_side = self._ns_cue_for_temperature(curr_temp, optimal_ref)
                if switched_now:
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, cue_type, 'n', current_state, 'state_switch']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if updated_cue_type != cue_type:
                    cue_type = updated_cue_type
                    correct_side = updated_correct_side
                    self.screen.display_temp_cue(cue_type)
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "CueUpdate", curr_temp, target_temp, cue_type, 'n', current_state, 'cue_update']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                poked_side = None
                if current_sensor[1] == 1 and choice_tuple[1] == 0:
                    choice_tuple[1] = 1
                    poked_side = 'l'
                    poke_time = time.time() - self.start_time
                    while self.sensor.get()[1] == 1:
                        pygame.time.wait(5)
                    choice_tuple[1] = 0
                elif current_sensor[3] == 1 and choice_tuple[3] == 0:
                    choice_tuple[3] = 1
                    poked_side = 'r'
                    poke_time = time.time() - self.start_time
                    while self.sensor.get()[3] == 1:
                        pygame.time.wait(5)
                    choice_tuple[3] = 0

                if poked_side is not None:
                    if poked_side == correct_side:
                        result = 'correct'
                        poke_pos = poked_side
                        new_target = self._ns_target_for_side(target_temp, poked_side, temp_change)
                        break
                    else:
                        print(f"  Wrong side ({poked_side}), ignoring.")
                        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                                  poke_time, "WrongPoke", curr_temp, target_temp, cue_type, poked_side, current_state, 'wrong']
                        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                pygame.time.wait(5)

            if result == 'session_end':
                self.screen.show()
                break

            if result == 'timeout':
                print(f"  Timeout. Cue removed.")
                self.screen.show()
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Timeout", curr_temp, target_temp, cue_type, 'n', current_state, 'timeout']
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                continue

            # 정답 처리
            print(f"  Correct poke: {poke_pos} | Temp: {curr_temp:.1f} -> {new_target:.1f}°C")
            self.reward.give(0.1)
            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))
            attenuation_active = False
            self.peltier_queue.put(("SET_TEMP", new_target))

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      poke_time, "CorrectPoke", curr_temp, new_target, cue_type, poke_pos, current_state, 'correct']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.screen.show(state=["w"])

            reached_target = False
            wait_start = time.time()
            while True:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                curr_temp, target_temp = self._get_shared_temperatures()
                if switched_now:
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, cue_type, 'n', current_state, 'state_switch']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if curr_temp is not None and abs(curr_temp - new_target) <= temp_tolerance:
                    reached_target = True
                    break
                if time.time() - wait_start > 60:
                    break
                pygame.time.wait(200)

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "TempReached" if reached_target else "TempTimeout", curr_temp, new_target, cue_type, poke_pos, current_state, 'correct' if reached_target else 'temp_timeout']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            # 온도 도달 후 30초간 attenuation 비활성화 유지 (비블로킹)
            att_resume_time = time.time() + 30
            print(f"Attenuation hold: resumes in 30s")
            self.screen.show()

        print("=== New Stage 2 Session Ended ===")

    def New_Stage3(self, start_temp=30.0, task_time=60,
                   attenuation_rate=0.07, temp_change=4.0, temp_tolerance=0.5,
                   optimal_ref=30.0, att_min=15.0, att_max=45.0, choice_timeout=120):
        """
        New Protocol Stage 3: 50% 정답만 cue / 50% 양쪽 cue 혼합
        - 정답만 trial: NS2와 동일 (오답 무시, 120초 timeout)
        - 양쪽 trial: 둘 다 유효, 어느 쪽 poke해도 온도 변화
        """
        print("=== New Protocol Stage 3: 50% Correct / 50% Both ===")

        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Cue_Type', 'Poke_pos', 'State', 'Trial_Type', 'Result']

        target_temp = start_temp
        self.peltier_queue.put(("SET_TEMP", target_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))

        current_state, attenuation_sign, state_switch_after_sec, state_switched = self._init_ns_attenuation_state()

        trial = 0
        start_Ex = time.time()
        attenuation_active = False

        print(f"Waiting for initial temperature: {target_temp}°C")
        while True:
            current_state, attenuation_sign, state_switched, _switched = self._maybe_switch_ns_attenuation_state(
                start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                state_switched, attenuation_rate, attenuation_active
            )
            curr_temp, _ = self._get_shared_temperatures()
            if curr_temp is not None and abs(curr_temp - target_temp) < 1.0:
                break
            if time.time() - start_Ex > 300:
                print(f"Initial temperature did not reach {target_temp} C. Last temperature: {curr_temp}")
                return
            pygame.time.wait(500)

        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
        attenuation_active = True
        self.screen.show()

        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', 'n', current_state, 'n', 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        att_resume_time = 0.0  # attenuation 재활성화 예약 시각 (0.0 = 즉시 가능)

        while (time.time() - start_Ex) < task_time * 60:
            if not attenuation_active and time.time() >= att_resume_time:
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                attenuation_active = True
            current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                state_switched, attenuation_rate, attenuation_active
            )
            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp
            if switched_now:
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, 'n', 'n', current_state, 'n', 'state_switch']
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.screen.display_start_cue_center()

            # Center poke 대기
            choice_tuple = [0, 0, 0, 0]
            center_poked = False
            while not center_poked:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                if (time.time() - start_Ex) >= task_time * 60:
                    break
                current_sensor = self.sensor.get()
                if switched_now:
                    curr_temp, target_temp = self._get_shared_temperatures()
                    if target_temp is None:
                        target_temp = start_temp
                    if curr_temp is None:
                        curr_temp = target_temp
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, 'n', 'n', current_state, 'n', 'state_switch']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if current_sensor[2] == 1 and choice_tuple[2] == 0:
                    choice_tuple[2] = 1
                    center_poked = True
                    while self.sensor.get()[2] == 1:
                        pygame.time.wait(5)
                    choice_tuple[2] = 0
                if not attenuation_active and time.time() >= att_resume_time:
                    self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                    attenuation_active = True
                pygame.time.wait(5)

            if not center_poked:
                break

            trial += 1
            center_poke_time = time.time() - self.start_time
            self.reward.give(0.1)

            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp

            correct_cue, correct_side = self._ns_cue_for_temperature(curr_temp, optimal_ref)

            # 50% 확률로 trial type 결정
            trial_type = 'both' if random.random() < 0.5 else 'correct_only'

            if trial_type == 'correct_only':
                self.screen.display_temp_cue(correct_cue)
                cue_type = correct_cue
            else:  # both
                self.screen.display_temp_both()
                cue_type = 'both'

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      center_poke_time, "CenterPoke", curr_temp, target_temp, cue_type, 'm', current_state, trial_type, 'n']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            print(f"Trial {trial} | Temp: {curr_temp:.1f}°C | Type: {trial_type} | Correct: {correct_side}")

            # Side poke 대기
            side_choice_start = time.time()
            choice_tuple = [0, 0, 0, 0]
            result = 'timeout'
            poke_pos = 'n'
            poke_time = center_poke_time
            new_target = target_temp

            while (time.time() - side_choice_start) < choice_timeout:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                if (time.time() - start_Ex) >= task_time * 60:
                    result = 'session_end'
                    break
                current_sensor = self.sensor.get()
                curr_temp, target_temp = self._get_shared_temperatures()
                if target_temp is None:
                    target_temp = start_temp
                if curr_temp is None:
                    curr_temp = target_temp
                updated_correct_cue, updated_correct_side = self._ns_cue_for_temperature(curr_temp, optimal_ref)
                if switched_now:
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, cue_type, 'n', current_state, trial_type, 'state_switch']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if updated_correct_side != correct_side:
                    correct_side = updated_correct_side
                    correct_cue = updated_correct_cue
                    if trial_type == 'correct_only':
                        cue_type = correct_cue
                        self.screen.display_temp_cue(cue_type)
                        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                                  time.time() - self.start_time, "CueUpdate", curr_temp, target_temp, cue_type, 'n', current_state, trial_type, 'cue_update']
                        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                poked_side = None
                if current_sensor[1] == 1 and choice_tuple[1] == 0:
                    choice_tuple[1] = 1
                    poked_side = 'l'
                    poke_time = time.time() - self.start_time
                    while self.sensor.get()[1] == 1:
                        pygame.time.wait(5)
                    choice_tuple[1] = 0
                elif current_sensor[3] == 1 and choice_tuple[3] == 0:
                    choice_tuple[3] = 1
                    poked_side = 'r'
                    poke_time = time.time() - self.start_time
                    while self.sensor.get()[3] == 1:
                        pygame.time.wait(5)
                    choice_tuple[3] = 0

                if poked_side is not None:
                    if trial_type == 'correct_only':
                        if poked_side == correct_side:
                            result = 'correct'
                            poke_pos = poked_side
                            new_target = self._ns_target_for_side(target_temp, poked_side, temp_change)
                            break
                        else:
                            print(f"  Wrong side ({poked_side}), ignoring.")
                            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                                      poke_time, "WrongPoke", curr_temp, target_temp, cue_type, poked_side, current_state, trial_type, 'wrong']
                            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                    else:  # both: 양쪽 모두 유효
                        result = 'correct' if poked_side == correct_side else 'wrong_but_valid'
                        poke_pos = poked_side
                        new_target = self._ns_target_for_side(target_temp, poked_side, temp_change)
                        break

                pygame.time.wait(5)

            if result == 'session_end':
                self.screen.show()
                break

            if result == 'timeout':
                self.screen.show()
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Timeout", curr_temp, target_temp, cue_type, 'n', current_state, trial_type, 'timeout']
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                continue

            # 온도 변화 처리
            print(f"  Poke: {poke_pos} | Result: {result} | Temp: {curr_temp:.1f} -> {new_target:.1f}°C")
            self.reward.give(0.1)
            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))
            attenuation_active = False
            self.peltier_queue.put(("SET_TEMP", new_target))

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      poke_time, "SidePoke", curr_temp, new_target, cue_type, poke_pos, current_state, trial_type, result]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.screen.show(state=["w"])

            reached_target = False
            wait_start = time.time()
            while True:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                curr_temp, target_temp = self._get_shared_temperatures()
                if switched_now:
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, cue_type, 'n', current_state, trial_type, 'state_switch']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if curr_temp is not None and abs(curr_temp - new_target) <= temp_tolerance:
                    reached_target = True
                    break
                if time.time() - wait_start > 60:
                    break
                pygame.time.wait(200)

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "TempReached" if reached_target else "TempTimeout", curr_temp, new_target, cue_type, poke_pos, current_state, trial_type, result if reached_target else 'temp_timeout']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            # 온도 도달 후 30초간 attenuation 비활성화 유지 (비블로킹)
            att_resume_time = time.time() + 30
            print(f"Attenuation hold: resumes in 30s")
            self.screen.show()

        print("=== New Stage 3 Session Ended ===")

    def New_Stage4(self, start_temp=30.0, task_time=60,
                   attenuation_rate=0.07, temp_change=4.0, temp_tolerance=0.5,
                   optimal_ref=30.0, att_min=15.0, att_max=45.0, choice_timeout=120):
        """
        New Protocol Stage 4: 항상 양쪽 cue (Full Both)
        - Center poke -> display_temp_both() -> 양쪽 모두 유효
        - NS3에서 'both' trial만 고정한 버전
        """
        print("=== New Protocol Stage 4: Always Both Cue ===")

        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Correct_side', 'Poke_pos', 'State', 'Result']

        target_temp = start_temp
        self.peltier_queue.put(("SET_TEMP", target_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))

        current_state, attenuation_sign, state_switch_after_sec, state_switched = self._init_ns_attenuation_state()

        trial = 0
        start_Ex = time.time()
        attenuation_active = False

        print(f"Waiting for initial temperature: {target_temp}°C")
        while True:
            current_state, attenuation_sign, state_switched, _switched = self._maybe_switch_ns_attenuation_state(
                start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                state_switched, attenuation_rate, attenuation_active
            )
            curr_temp, _ = self._get_shared_temperatures()
            if curr_temp is not None and abs(curr_temp - target_temp) < 1.0:
                break
            if time.time() - start_Ex > 300:
                print(f"Initial temperature did not reach {target_temp} C. Last temperature: {curr_temp}")
                return
            pygame.time.wait(500)

        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
        attenuation_active = True
        self.screen.show()

        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', 'n', current_state, 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        att_resume_time = 0.0  # attenuation 재활성화 예약 시각 (0.0 = 즉시 가능)

        while (time.time() - start_Ex) < task_time * 60:
            if not attenuation_active and time.time() >= att_resume_time:
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                attenuation_active = True
            current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                state_switched, attenuation_rate, attenuation_active
            )
            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp
            if switched_now:
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, 'n', 'n', current_state, 'state_switch']
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.screen.display_start_cue_center()

            # Center poke 대기
            choice_tuple = [0, 0, 0, 0]
            center_poked = False
            while not center_poked:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                if (time.time() - start_Ex) >= task_time * 60:
                    break
                current_sensor = self.sensor.get()
                if switched_now:
                    curr_temp, target_temp = self._get_shared_temperatures()
                    if target_temp is None:
                        target_temp = start_temp
                    if curr_temp is None:
                        curr_temp = target_temp
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, 'n', 'n', current_state, 'state_switch']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if current_sensor[2] == 1 and choice_tuple[2] == 0:
                    choice_tuple[2] = 1
                    center_poked = True
                    while self.sensor.get()[2] == 1:
                        pygame.time.wait(5)
                    choice_tuple[2] = 0
                if not attenuation_active and time.time() >= att_resume_time:
                    self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                    attenuation_active = True
                pygame.time.wait(5)

            if not center_poked:
                break

            trial += 1
            center_poke_time = time.time() - self.start_time
            self.reward.give(0.1)

            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp

            _, correct_side = self._ns_cue_for_temperature(curr_temp, optimal_ref)

            self.screen.display_temp_both()

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      center_poke_time, "CenterPoke", curr_temp, target_temp, correct_side, 'm', current_state, 'n']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            print(f"Trial {trial} | Temp: {curr_temp:.1f}°C | Correct side: {correct_side} (both shown)")

            # Side poke 대기
            side_choice_start = time.time()
            choice_tuple = [0, 0, 0, 0]
            result = 'timeout'
            poke_pos = 'n'
            poke_time = center_poke_time
            new_target = target_temp

            while (time.time() - side_choice_start) < choice_timeout:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                if (time.time() - start_Ex) >= task_time * 60:
                    result = 'session_end'
                    break
                current_sensor = self.sensor.get()
                curr_temp, target_temp = self._get_shared_temperatures()
                if target_temp is None:
                    target_temp = start_temp
                if curr_temp is None:
                    curr_temp = target_temp
                _, updated_correct_side = self._ns_cue_for_temperature(curr_temp, optimal_ref)
                if switched_now:
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, correct_side, 'n', current_state, 'state_switch']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if updated_correct_side != correct_side:
                    correct_side = updated_correct_side
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "CorrectSideUpdate", curr_temp, target_temp, correct_side, 'n', current_state, 'cue_update']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                poked_side = None
                if current_sensor[1] == 1 and choice_tuple[1] == 0:
                    choice_tuple[1] = 1
                    poked_side = 'l'
                    poke_time = time.time() - self.start_time
                    while self.sensor.get()[1] == 1:
                        pygame.time.wait(5)
                    choice_tuple[1] = 0
                elif current_sensor[3] == 1 and choice_tuple[3] == 0:
                    choice_tuple[3] = 1
                    poked_side = 'r'
                    poke_time = time.time() - self.start_time
                    while self.sensor.get()[3] == 1:
                        pygame.time.wait(5)
                    choice_tuple[3] = 0

                if poked_side is not None:
                    result = 'correct' if poked_side == correct_side else 'incorrect'
                    poke_pos = poked_side
                    new_target = self._ns_target_for_side(target_temp, poked_side, temp_change)
                    break

                pygame.time.wait(5)

            if result == 'session_end':
                self.screen.show()
                break

            if result == 'timeout':
                self.screen.show()
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Timeout", curr_temp, target_temp, correct_side, 'n', current_state, 'timeout']
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                continue

            print(f"  Poke: {poke_pos} | Result: {result} | Temp: {curr_temp:.1f} -> {new_target:.1f}°C")
            self.reward.give(0.1)
            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))
            attenuation_active = False
            self.peltier_queue.put(("SET_TEMP", new_target))

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      poke_time, "SidePoke", curr_temp, new_target, correct_side, poke_pos, current_state, result]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            self.screen.show(state=["w"])

            reached_target = False
            wait_start = time.time()
            while True:
                current_state, attenuation_sign, state_switched, switched_now = self._maybe_switch_ns_attenuation_state(
                    start_Ex, current_state, attenuation_sign, state_switch_after_sec,
                    state_switched, attenuation_rate, attenuation_active
                )
                curr_temp, target_temp = self._get_shared_temperatures()
                if switched_now:
                    dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                              time.time() - self.start_time, "StateSwitch", curr_temp, target_temp, correct_side, 'n', current_state, 'state_switch']
                    self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                if curr_temp is not None and abs(curr_temp - new_target) <= temp_tolerance:
                    reached_target = True
                    break
                if time.time() - wait_start > 60:
                    break
                pygame.time.wait(200)

            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "TempReached" if reached_target else "TempTimeout", curr_temp, new_target, correct_side, poke_pos, current_state, result if reached_target else 'temp_timeout']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            # 온도 도달 후 30초간 attenuation 비활성화 유지 (비블로킹)
            att_resume_time = time.time() + 30
            print(f"Attenuation hold: resumes in 30s")
            self.screen.show()

        print("=== New Stage 4 Session Ended ===")


speed_test = 0.25
ITI_test = 20
trial_test = 30

class Temperature_test(Task):
    def task(self):
        self.Temperature_test(stay_time_start=1, task_time=180, stay_time_end=60, start_temp = 10, max_temp = 40, min_temp = 10, d_temp = 4, ITI_duration = 0, FP_on=False, hold_seconds=60, reach_tolerance=0.5)

class Cold_to_hot_block(Task):
    def task(self):
        self.Cold_to_hot_block(stay_time_start=1, task_time=60, stay_time_end=30, start_temp = 10, max_temp = 40, min_temp = 10, d_temp = 5, ITI_duration = 0, FP_on=True)

class Hot_to_cold_block(Task):
    def task(self):
        self.Hot_to_cold_block(stay_time_start=1, task_time=60, stay_time_end=30, start_temp = 40, max_temp = 40, min_temp = 10, d_temp = 5, ITI_duration = 0, FP_on=True)

class Cold_to_hot_block_2(Task):
    def task(self):
        self.Cold_to_hot_block(stay_time_start=1, task_time=60, stay_time_end=30, start_temp = 10, max_temp = 25, min_temp = 10, d_temp = 5, ITI_duration = 0, FP_on=True,
                               poke_temp= 2.0, optimal_temp= 30.0)

class Hot_to_cold_block_2(Task):
    def task(self):
        self.Hot_to_cold_block(stay_time_start=1, task_time=60, stay_time_end=30, start_temp = 40, max_temp = 40, min_temp = 15, d_temp = 5, ITI_duration = 0, FP_on=True,
                               poke_temp= -2.0, optimal_temp= 30.0)

class Find_optimal_block(Task):
    def task(self):
        self.Find_optimal_block(stay_time_start=1, task_time=60, stay_time_end=30, start_temp = 29.9, max_temp = 40, min_temp = 10, d_temp = 5, ITI_duration = 0, FP_on=True)

class Preference_test_cold(Task):
    def task(self):
        self.Preference_test(stay_time_start=1, task_time=5, stay_time_end=30, start_temp = 5, max_temp = 45, min_temp = 5, optimal_temp= 25, d_temp = 5, ITI_duration = 0, FP_on=True)

class Preference_test_hot(Task):
    def task(self):
        self.Preference_test(stay_time_start=1, task_time=5, stay_time_end=30, start_temp = 45, max_temp = 45, min_temp = 5, optimal_temp= 25, d_temp = 5, ITI_duration = 0, FP_on=True)

class POA_task(Task):
    def task(self):
        self.POA_task(stay_time_start=1, task_time=60, stay_time_end=30, start_temp = 29.9, max_temp = 40, min_temp = 10, d_temp = 5, ITI_duration = 0, FP_on=True)

# ============================================================
# Training Protocol Task Classes
# ============================================================

class Training_Stage1_Cold(Task):
    """Stage 1: 시작 온도 23°C (차가운 쪽에서 시작)"""
    def task(self):
        self.Training_Stage1(start_temp=20.0)

class Training_Stage1_Hot(Task):
    """Stage 1: 시작 온도 37°C (뜨거운 쪽에서 시작)"""
    def task(self):
        self.Training_Stage1(start_temp=40.0)

class Training_Stage2_Cold(Task):
    """Stage 2: 시작 온도 25°C (optimal 경계 차가운 쪽)"""
    def task(self):
        self.Training_Stage2(start_temp=20.0)

class Training_Stage2_Hot(Task):
    """Stage 2: 시작 온도 35°C (optimal 경계 뜨거운 쪽)"""
    def task(self):
        self.Training_Stage2(start_temp=40.0)

class Training_Stage3_Full(Task):
    """Stage 3: Full Task (시작 온도 30°C, optimal 중앙)"""
    def task(self):
        self.Training_Stage3(start_temp=30.0)

# ============================================================
# New Protocol Task Classes (NS1 ~ NS4)
# ============================================================

class New_Stage1_Task(Task):
    """New Protocol Stage 1: Center poke → Temp change (center 정답 cue)"""
    def task(self):
        self.New_Stage1()

class New_Stage2_Task(Task):
    """New Protocol Stage 2: Center → Side cue → Temp change"""
    def task(self):
        self.New_Stage2()

class New_Stage3_Task(Task):
    """New Protocol Stage 3: 50% correct_only / 50% both 혼합"""
    def task(self):
        self.New_Stage3()

class New_Stage4_Task(Task):
    """New Protocol Stage 4: 항상 양쪽 cue (Full Both)"""
    def task(self):
        self.New_Stage4()

class TRL1_Task(Task):
    """TRL1: Center-only; bump → white until chamber reaches bump target → drift 0.07°C/s."""
    def task(self):
        self.TRL1()

class TRL2_Task(Task):
    """TRL2: 좌=cold, 우=hot 고정; 한 번에 한쪽만 큐, 번갈아 제시."""
    def task(self):
        self.TRL2()

class TRL3_Task(Task):
    """TRL3: TRL_main과 동일 로직 + 단일/양쪽 확률 혼합; single은 att 방향 반대 큐; both 연속 최대 3회."""

    def __init__(
        self,
        json_dir,
        Video_file_name,
        FrameTime_file_name,
        TrialData_file_name,
        mouseid,
        session,
        shared_data,
        dict_lock,
        start_time,
        peltier_queue,
        stop_event,
        both_cue_percent=50.0,
    ):
        self.both_cue_percent = max(0.0, min(100.0, float(both_cue_percent)))
        super().__init__(
            json_dir,
            Video_file_name,
            FrameTime_file_name,
            TrialData_file_name,
            mouseid,
            session,
            shared_data,
            dict_lock,
            start_time,
            peltier_queue,
            stop_event,
        )

    def task(self):
        single_cue_probability = 1.0 - (self.both_cue_percent / 100.0)
        self.TRL3(single_cue_probability=single_cue_probability)

class TRL_main_Task(Task):
    """TRL_main: 항상 양쪽 큐; 범프·감쇠는 구 TRL3와 동일."""
    def task(self):
        self.TRL_main()

class TL1_Task(Task):
    """TL1: left-only, choice 시 +5°C, no-choice 시 -5°C, 20s/40s window."""
    def task(self):
        self.TL1()

class TL2_Task(Task):
    """TL2: left-only, choice 시 +3/3.5/4°C, no-choice 시 -1.5/-2/-2.5°C."""
    def task(self):
        self.TL2()

if __name__ =="__main__":
    json_dir = input("Enter your json file\n")
    while (1):
        task = input("""
To start enter number you want to run
    [1] Magazine training
    [2] Center Hole FR1
    [3] Chaining Center to Left and Right
    [4] Responding on Sides Only:
    [5] Responding on Chained Sides Followed by ITI
    [6] Improving and Worsening Delay Discounding
    [7] Worsening and Improving Probability Discounting
    [0] Exit
""")
        if task == "0":
            print("Selected Exit")
            break
        elif task == "1":
            instance = Pretraining_1(json_dir)
            instance.run()
            break
        elif task == "2":
            instance = Pretraining_2(json_dir)
            instance.run()
            break
        elif task == "3":
            instance = Pretraining_3(json_dir)
            instance.run()
            break
        elif task == "4":
            instance = Pretraining_4(json_dir)
            instance.run()
            break
        elif task == "5":
            instance = Length_Amount_Association(json_dir)
            instance.run()
            break
        elif task == "6":
            instance = PastPresentFutureTest(json_dir)
            instance.run()
            break
        else:
            print("Wrong input, please try again\n")
    print("Proccess made an end\nShutting down...\n")
    sys.exit()

