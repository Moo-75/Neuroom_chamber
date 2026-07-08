# -*- coding: utf-8 -*-
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

CAMERA_INDEX = 0
CAMERA_FPS = 30.0
CAMERA_READ_RETRY_DELAY_SEC = 0.1
# 실패 read 한 번이 V4L2 select 타임아웃(~10초)만큼 블로킹될 수 있으므로,
# 재연결을 빠르게 시도하도록 임계값을 작게 잡는다.
CAMERA_REOPEN_AFTER_FAILURES = 3
SENSOR_POLL_WAIT_MS = 50


# ============================================================
# 카메라 / 녹화 헬퍼 + 워커 (별도 프로세스에서 실행)
#
# 녹화를 태스크와 같은 프로세스의 스레드로 돌리면, 태스크 루프의
# 빈번한 sleep 없는 폴링 루프가 GIL을 독점해 녹화 스레드가 굶고
# 카메라 서비스가 밀려 V4L2 select() timeout(프레임 끊김)이 발생한다.
# 따라서 peltier_worker/sensor_worker와 동일하게 독립 프로세스로 분리한다.
# ============================================================

def _camera_backend():
    if sys.platform.startswith("linux") and hasattr(cv2, "CAP_V4L2"):
        return cv2.CAP_V4L2
    return getattr(cv2, "CAP_ANY", 0)


def _open_camera(target_fps=CAMERA_FPS):
    backend = _camera_backend()
    if backend:
        cap = cv2.VideoCapture(CAMERA_INDEX, backend)
    else:
        cap = cv2.VideoCapture(CAMERA_INDEX)
    if not cap.isOpened():
        cap.release()
        return None, None, None, None

    if hasattr(cv2, "CAP_PROP_FOURCC"):
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FPS, target_fps)
    if hasattr(cv2, "CAP_PROP_BUFFERSIZE"):
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    fps = target_fps
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if width <= 0 or height <= 0:
        for _ in range(5):
            ret, frame = cap.read()
            if ret and frame is not None:
                height, width = frame.shape[:2]
                break
            time.sleep(CAMERA_READ_RETRY_DELAY_SEC)

    if width <= 0 or height <= 0:
        cap.release()
        return None, None, None, None

    return cap, width, height, fps


def _gst_safe_path(path):
    return os.path.abspath(path).replace("\\", "\\\\").replace('"', '\\"')


def _ensure_parent_dir(path):
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)


def _open_video_writer(video_file_name, fps, width, height):
    _ensure_parent_dir(video_file_name)
    bitrate_kbps = max(2000, min(8000, int(width * height * fps * 0.00025)))

    if sys.platform.startswith("linux") and hasattr(cv2, "CAP_GSTREAMER"):
        fps_num = max(1, int(round(fps)))
        pipeline = (
            "appsrc is-live=true format=time ! "
            f"video/x-raw,format=BGR,width={width},height={height},framerate={fps_num}/1 ! "
            "videoconvert ! video/x-raw,format=I420 ! "
            f"x264enc speed-preset=ultrafast tune=zerolatency bitrate={bitrate_kbps} key-int-max={int(fps * 2)} ! "
            "h264parse config-interval=-1 ! mp4mux ! "
            f'filesink location="{_gst_safe_path(video_file_name)}"'
        )
        writer = cv2.VideoWriter(pipeline, cv2.CAP_GSTREAMER, 0, fps, (width, height), True)
        if writer.isOpened():
            return writer, f"H.264/GStreamer ({bitrate_kbps} kbps)"
        writer.release()

    writer = cv2.VideoWriter(
        video_file_name,
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (width, height),
    )
    if writer.isOpened():
        return writer, "mp4v/OpenCV"
    writer.release()
    return None, None


def _reopen_camera(cap, target_fps=CAMERA_FPS):
    if cap is not None:
        cap.release()
    time.sleep(0.5)
    return _open_camera(target_fps)


def _cam_log(msg):
    # 타임스탬프 + 즉시 flush: dmesg -wT(커널 로그)와 초 단위로 대조하기 위함.
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def video_record_worker(video_file_name, target_fps, shared_data, dict_lock, start_time, stop_event):
    """독립 프로세스에서 카메라를 열고 stop_event가 설정될 때까지 녹화한다."""
    cap, width, height, fps = _open_camera(target_fps)
    if cap is None:
        _cam_log("[Camera] camera open failed; task will continue without video recording.")
        return
    out, writer_name = _open_video_writer(video_file_name, fps, width, height)
    if out is None:
        _cam_log("[Camera] MP4 writer open failed; task will continue without video recording.")
        cap.release()
        return
    _cam_log(f"[Camera] recording {width}x{height} @ {fps:.2f} fps to MP4 via {writer_name}")

    frame_count = 0
    curr_temp = 0.0
    consecutive_failures = 0
    warned_resize = False
    # 온도 텍스트를 해상도에 맞춰 오른쪽에 배치 (화면 밖으로 나가는 것 방지)
    temp_text_x = max(10, int(width) - 150) if width else 10
    # 프레임 페이싱: 카메라가 목표 fps보다 빠르게 프레임을 줘도 writer 선언 fps에
    # 맞춰 기록해야 저장된 MP4가 실시간 속도로 재생된다.
    frame_interval = 1.0 / fps if fps and fps > 0 else 0.0
    next_write_time = time.time()

    # 진단용 하트비트: 10초마다 실제 기록 fps를 찍어 스톨 직전 처리량 저하를 본다.
    hb_interval = 10.0
    hb_last_time = time.time()
    hb_last_count = 0
    read_fail_total = 0

    try:
        while not stop_event.is_set():
            read_start = time.time()
            ret, frame = cap.read()
            read_elapsed = time.time() - read_start
            if not ret or frame is None:
                consecutive_failures += 1
                read_fail_total += 1
                if consecutive_failures == 1 or consecutive_failures % 10 == 0:
                    _cam_log(
                        f"[Camera] frame read failed ({consecutive_failures} consecutive, "
                        f"blocked {read_elapsed:.1f}s, total fails {read_fail_total}); retrying."
                    )
                if consecutive_failures % CAMERA_REOPEN_AFTER_FAILURES == 0:
                    new_cap, w, h, f = _reopen_camera(cap, target_fps)
                    if new_cap is not None:
                        cap = new_cap
                        width, height = w, h
                        temp_text_x = max(10, int(width) - 150) if width else 10
                        consecutive_failures = 0
                        _cam_log(f"[Camera] reopened camera at {w}x{h} @ {f:.2f} fps")
                    else:
                        cap = None
                        _cam_log("[Camera] camera reopen failed; will keep retrying.")
                time.sleep(CAMERA_READ_RETRY_DELAY_SEC)
                if cap is None:
                    new_cap, w, h, f = _open_camera(target_fps)
                    if new_cap is not None:
                        cap, width, height = new_cap, w, h
                        temp_text_x = max(10, int(width) - 150) if width else 10
                        consecutive_failures = 0
                        _cam_log(f"[Camera] reopened camera at {w}x{h} @ {f:.2f} fps")
                continue
            consecutive_failures = 0

            # 목표 fps보다 이르게 도착한 프레임은 버려 기록 속도를 일정하게 유지.
            now = time.time()
            if frame_interval:
                if now < next_write_time:
                    continue
                next_write_time += frame_interval
                # 루프가 뒤처졌을 때 한꺼번에 몰아쓰지 않도록 스케줄을 재동기화.
                if now > next_write_time + frame_interval:
                    next_write_time = now + frame_interval

            if frame.shape[1] != width or frame.shape[0] != height:
                if not warned_resize:
                    _cam_log(
                        "[Camera] frame size changed; resizing frames to the "
                        "initial video size for a valid MP4 stream."
                    )
                    warned_resize = True
                frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)

            if frame_count % 5 == 0:
                try:
                    with dict_lock:
                        curr_temp = shared_data["average_temp"]
                except Exception as e:
                    _cam_log(f"[Camera] temperature read failed: {e}; keeping last known value.")
                if curr_temp is None or not math.isfinite(curr_temp):
                    curr_temp = float("nan")

            unix_timestamp = time.time() - start_time
            timestamp_str = f"{unix_timestamp:.3f}"
            cv2.putText(frame, timestamp_str, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.putText(frame, f"{curr_temp:.3f}", (temp_text_x, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

            try:
                out.write(frame)
            except Exception as e:
                _cam_log(f"[Camera] video write failed: {e}; stopping video recording only.")
                break

            frame_count += 1

            now = time.time()
            if now - hb_last_time >= hb_interval:
                eff_fps = (frame_count - hb_last_count) / (now - hb_last_time)
                _cam_log(f"[Camera] heartbeat: {frame_count} frames written, {eff_fps:.1f} fps recently")
                hb_last_time = now
                hb_last_count = frame_count
    except Exception as e:
        _cam_log(f"[Camera] recording loop error: {e}; stopping video recording only.")
    finally:
        if out is not None:
            out.release()
        if cap is not None:
            cap.release()

# # maze?먯꽌 ?ъ슜??library瑜????⑥빞?섎뒗寃??꾨땶媛?
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

        # 녹화는 run()에서 별도 프로세스(video_record_worker)로 실행한다.
        self.video_file_name = Video_file_name
        self.video_stop_event = multiprocessing.Event()

    def task(self):
        pass

    def _temp_status_monitor(self, stop_event, interval_sec=30.0):
        """?곕?????以?\\r)???꾩옱/紐⑺몴 ?⑤룄瑜?二쇨린?곸쑝濡?媛깆떊."""
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
        status_stop = threading.Event()
        self.video_stop_event.clear()
        video_proc = multiprocessing.Process(
            target=video_record_worker,
            args=(
                self.video_file_name,
                CAMERA_FPS,
                self.shared_data,
                self.dict_lock,
                self.start_time,
                self.video_stop_event,
            ),
        )
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
            self.video_stop_event.set()
            # 블로킹 read(~10초)가 끝나야 워커가 stop_event를 확인하므로 넉넉히 대기.
            video_proc.join(timeout=12)
            if video_proc.is_alive():
                print("[Warning] video process did not stop within 12s; terminating it.")
                video_proc.terminate()
                video_proc.join(timeout=5)

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
        - 紐⑺몴: Nose poke媛 ?⑤룄 蹂?붾? ?좊컻?쒕떎??寃껋쓣 ?숈뒿
        - Attenuation: OFF
        - State: ?놁쓬
        - 醫낅즺 議곌굔: Optimal ?⑤룄(30?? ?꾨떖 ??10遺?寃쎄낵 ??醫낅즺
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

        # 珥덇린 ?ㅼ젙
        target_temp = start_temp
        self.peltier_queue.put(("SET_TEMP", target_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))  # Attenuation OFF

        optimal_reached_time = None
        is_start_cold = start_temp < optimal_threshold

        trial = 0
        start_Ex = time.time()

        # 珥덇린 ?⑤룄 ?꾨떖 ?湲?
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

        # Trial 猷⑦봽
        while trial < max_trials and (time.time() - start_Ex) < task_time * 60:
            trial += 1
            print(f"\n--- Trial {trial} ---")

            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp

            # Optimal ?꾨떖 泥댄겕 諛?醫낅즺 ??대㉧ ?뺤씤
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

            # 1. Trial Cue (?뚮━留? wait ?놁쓬)
            self.reward.give(0.1)  # 諛몃툕 ?뚮━ cue
            self.reward.give(0.1)  # 諛몃툕 ?뚮━ cue
            cue_time = time.time() - self.start_time
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      cue_time, "TrialCue", curr_temp, target_temp, 'n', 0]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            # 2. Choice Window - ?묒そ cue ?쒖떆
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

                pygame.time.wait(SENSOR_POLL_WAIT_MS)

            # 3. Choice 泥섎━ + Outcome Phase
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

                # Outcome: ?⑤룄 ?곸듅 諛⑺뼢 ?쒖떆
                self.screen.show(state=["warm"])

                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_L_Hot", curr_temp, new_target, 'l', response_time]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                # ?⑤룄 蹂???쒓컙
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

                # Outcome: ?⑤룄 ?섍컯 諛⑺뼢 ?쒖떆
                self.screen.show(state=["cool"])

                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_R_Cool", curr_temp, new_target, 'r', response_time]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                # ?⑤룄 蹂???쒓컙
                pygame.time.wait(int(temp_hold_time * 1000))

            else:  # No choice
                print("No Choice - Temperature maintained")

                # Outcome: ?붾㈃ OFF (no choice feedback)
                self.screen.show()

                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "NoChoice", curr_temp, target_temp, 'n', 0]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                # ?⑤룄 ?좎? ?쒓컙
                pygame.time.wait(int(temp_hold_time * 1000))

            # 4. ITI - ?붾㈃ OFF
            self.screen.show()
            pygame.time.wait(int(iti_duration * 1000))

            with self.dict_lock:
                curr_temp = self.shared_data["average_temp"]

            # Optimal ?꾨떖 泥댄겕 (?ш린?쒕룄 ??踰???濡쒓렇 李띿뼱以?
            if optimal_min <= curr_temp <= optimal_max:
                print(f"Current temp {curr_temp:.1f}°C is in optimal range!")

        # ?몄뀡 醫낅즺
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
        - 紐⑺몴: Attenuation怨?State 媛쒕뀗 ?숈뒿
        - Attenuation: ON (0.02°C/珥?
        - State: ?덉쓬 (?붾㈃ cue ?쒓났)
        - Attenuation? no choice 諛쒖깮 ?쒖젏遺???ㅼ쓬 choice源뚯? 吏???곸슜
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

        # 珥덇린 ?ㅼ젙
        target_temp = start_temp
        self.peltier_queue.put(("SET_TEMP", target_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))  # 珥덇린?먮뒗 attenuation OFF

        # State: 'hot' = ?⑤룄 ?곸듅 諛⑺뼢, 'cold' = ?⑤룄 ?섍컯 諛⑺뼢
        # 2026-02-10 Modify: Start state based on start_temp
        if target_temp < optimal_min:
            current_state = 'cold'
        elif target_temp > optimal_max:
            current_state = 'hot'
        else:
            current_state = random.choice(['hot', 'cold'])
        attenuation_active = False  # no choice 諛쒖깮 ??True, choice 諛쒖깮 ??False

        trial = 0
        start_Ex = time.time()

        # 珥덇린 ?⑤룄 ?꾨떖 ?湲?
        print(f"Waiting for initial temperature: {target_temp}°C")
        reached_target, curr_temp = self._wait_for_target_temperature(
            target_temp, tolerance=1.0, timeout_sec=300, poll_ms=500
        )
        if not reached_target:
            print(f"Initial temperature did not reach {target_temp} C. Last temperature: {curr_temp}")
            return

        # State cue ?쒖떆 (Stage 2?먯꽌???몄뀡 ?쒖옉 ??State ?쒖떆)
        self.screen.display_temp_cue(current_state)
        print(f"Initial State: {current_state}")

        # was_outside_optimal 珥덇린??(?ㅼ젣 ?⑤룄 湲곕컲)
        was_outside_optimal = curr_temp < optimal_min or curr_temp > optimal_max

        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        # Trial 猷⑦봽
        while trial < max_trials and (time.time() - start_Ex) < task_time * 60:
            trial += 1
            print(f"\n--- Trial {trial} (State: {current_state}, Attenuation: {attenuation_active}) ---")

            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp

            # Optimal 諛뽰뿉 ?덈뒗吏 泥댄겕 (?ㅼ쓬 trial???꾪빐 ?낅뜲?댄듃??trial ?앹뿉???섑뻾)

            # 1. Trial Cue (?뚮━留? wait ?놁쓬)
            self.reward.give(0.1)
            self.reward.give(0.1)

            cue_time = time.time() - self.start_time
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      cue_time, "TrialCue", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            # 2. Choice Window - ?묒そ cue ?쒖떆
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

                pygame.time.wait(SENSOR_POLL_WAIT_MS)
            with self.dict_lock:
                prev_temp = self.shared_data["target_temp"]

            # 3. Choice 泥섎━ + Outcome Phase
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

                # Choice 諛쒖깮 ??Attenuation OFF
                attenuation_active = False
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))

                # Outcome: ?⑤룄 ?곸듅 諛⑺뼢 ?쒖떆
                self.screen.show(state=["warm"])

                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_L_Hot", curr_temp, new_target, 'l', response_time, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                # State ?꾪솚 泥댄겕: optimal 諛???optimal ?덉쑝濡?吏꾩엯
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

                # Choice 諛쒖깮 ??Attenuation OFF
                attenuation_active = False
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))

                # Outcome: ?⑤룄 ?섍컯 諛⑺뼢 ?쒖떆
                self.screen.show(state=["cool"])

                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_R_Cool", curr_temp, new_target, 'r', response_time, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                # State ?꾪솚 泥댄겕
                if was_outside_optimal and optimal_min <= new_target <= optimal_max:
                    if random.random() < state_change_prob:
                        current_state = 'hot' if prev_temp < optimal_min else 'cold'
                    else:
                        current_state = random.choice(['hot', 'cold'])
                    print(f"State changed to: {current_state}")

                pygame.time.wait(int(temp_hold_time * 1000))

            else:  # No choice
                print("No Choice - Attenuation activated")

                # No choice ??Attenuation ON
                attenuation_active = True
                attenuation_direction = attenuation_rate if current_state == 'hot' else -attenuation_rate
                self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_direction))

                # Outcome: Attenuation 諛⑺뼢 ?쒖떆
                self.screen.show()

                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "NoChoice", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                pygame.time.wait(int(temp_hold_time * 1000))

            # 4. ITI - ?붾㈃ OFF (Attenuation? 怨꾩냽 ?좎?)
            self.screen.show()
            pygame.time.wait(int(iti_duration * 1000))

            # optimal 諛??곹깭 ?낅뜲?댄듃
            with self.dict_lock:
                curr_temp = self.shared_data["average_temp"]
            was_outside_optimal = curr_temp < optimal_min or curr_temp > optimal_max

        # ?몄뀡 醫낅즺
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
        - 紐⑺몴: State cue ?놁씠 Full task ?섑뻾
        - Attenuation: ON (0.03°C/珥?
        - State: ?덉쓬 (cue ?놁쓬 - 留덉슦?ㅺ? 異붾줎)
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

        # 珥덇린 ?ㅼ젙
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

        # 珥덇린 ?⑤룄 ?꾨떖 ?湲?
        print(f"Waiting for initial temperature: {target_temp}°C")
        while True:
            with self.dict_lock:
                curr_temp = self.shared_data["average_temp"]
            if curr_temp is not None and abs(curr_temp - target_temp) < 1.0:
                break
            pygame.time.wait(500)

        # Stage 3: State cue ?쒖떆 ????
        self.screen.show()
        print(f"Initial State (hidden): {current_state}")

        # was_outside_optimal 珥덇린??(?ㅼ젣 ?⑤룄 湲곕컲)
        was_outside_optimal = curr_temp < optimal_min or curr_temp > optimal_max

        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        # Trial 猷⑦봽
        while trial < max_trials and (time.time() - start_Ex) < task_time * 60:
            trial += 1
            print(f"\n--- Trial {trial} (State: {current_state} [hidden], Attenuation: {attenuation_active}) ---")

            curr_temp, target_temp = self._get_shared_temperatures()
            if target_temp is None:
                target_temp = start_temp
            if curr_temp is None:
                curr_temp = target_temp

            currently_outside_optimal = curr_temp < optimal_min or curr_temp > optimal_max

            # 1. Trial Cue (?뚮━留? wait ?놁쓬) - Stage 3: State cue ?놁쓬
            self.reward.give(0.1)
            self.reward.give(0.1)

            cue_time = time.time() - self.start_time
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      cue_time, "TrialCue", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            # 2. Choice Window - ?묒そ cue ?쒖떆
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

                pygame.time.wait(SENSOR_POLL_WAIT_MS)

            # 3. Choice 泥섎━ + Outcome Phase
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

                # Outcome: ?⑤룄 ?곸듅 諛⑺뼢 ?쒖떆
                self.screen.show(state=["warm"])

                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_L_Hot", curr_temp, new_target, 'l', response_time, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                # State ?꾪솚 泥댄겕
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

                # Outcome: ?⑤룄 ?섍컯 諛⑺뼢 ?쒖떆
                self.screen.show(state=["cool"])

                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Choice_R_Cool", curr_temp, new_target, 'r', response_time, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                # State ?꾪솚 泥댄겕
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

                # Outcome: Attenuation 諛⑺뼢 ?쒖떆 (Stage 3?먯꽌??留덉슦?ㅺ? 異붾줎?댁빞 ?섏?留??쒓컖???쇰뱶諛??쒓났)
                self.screen.show()

                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "NoChoice", curr_temp, target_temp, 'n', 0, current_state, attenuation_active]
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

                pygame.time.wait(int(temp_hold_time * 1000))

            # 4. ITI - ?붾㈃ OFF
            self.screen.show()
            pygame.time.wait(int(iti_duration * 1000))

            with self.dict_lock:
                curr_temp = self.shared_data["average_temp"]
            was_outside_optimal = curr_temp < optimal_min or curr_temp > optimal_max

        # ?몄뀡 醫낅즺
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
        - Center???꾩옱 ?⑤룄 湲곕컲?쇰줈 ??긽 ?뺣떟 cue瑜??쒖떆
        - 留덉슦?ㅺ? center瑜?poke?섎㈃ 利됱떆 ?⑤룄 蹂??
        - Attenuation ?덉쓬 (0.02??珥?
        """
        print("=== New Protocol Stage 1: Center Poke -> Temp Change ===")

        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Poke_count', 'Time', 'Event',
                       'Current_Temp', 'Target_Temp', 'Cue_Type', 'State']

        target_temp = start_temp
        self.peltier_queue.put(("SET_TEMP", target_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))  # ?湲?以??쇰떒 OFF

        # Day 湲곗? state 寃곗젙 (???cold start, 吏앹닔=hot start)
        current_state, attenuation_sign, state_switch_after_sec, state_switched = self._init_ns_attenuation_state()

        poke_count = 0
        start_Ex = time.time()
        attenuation_active = False

        # 珥덇린 ?⑤룄 ?꾨떖 ?湲?
        print(f"Waiting for initial temperature: {target_temp}°C")
        while True:
            with self.dict_lock:
                curr_temp = self.shared_data["average_temp"]
            if curr_temp is not None and abs(curr_temp - target_temp) < 1.0:
                break
            pygame.time.wait(500)

        # Attenuation ?쒖옉
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
        attenuation_active = True

        dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', current_state]
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        print(f"State: {current_state}, Attenuation: {attenuation_rate * attenuation_sign:+.4f}°C/s")

        att_resume_time = 0.0  # attenuation ?ы솢?깊솕 ?덉빟 ?쒓컖 (0.0 = 利됱떆 媛??

        # 硫붿씤 猷⑦봽
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

            # Center poke ?湲?
            choice_tuple = [0, 0, 0, 0]
            center_poked = False
            switched_now = False  # inner loop 吏꾩엯 ??珥덇린??(outer loop ?댁쨷 湲곕줉 諛⑹?)
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
                    # ?쇱꽌?먯꽌 ?섏삱 ?뚭퉴吏 ?湲?
                    while self.sensor.get()[2] == 1:
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    choice_tuple[2] = 0
                if not attenuation_active and time.time() >= att_resume_time:
                    self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                    attenuation_active = True
                pygame.time.wait(SENSOR_POLL_WAIT_MS)

            if not center_poked:
                break  # ?몄뀡 醫낅즺

            print(f"  Center poke #{poke_count}! (Cue was: {cue_type.upper()})")

            # Sound cue
            self.reward.give(0.1)

            # Attenuation OFF (?⑤룄 蹂???숈븞 ?쇱떆 以묐떒)
            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", 0))
            attenuation_active = False

            # ?⑤룄 蹂??吏??
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

            # ???붾㈃ (?⑤룄 蹂??以??湲?
            self.screen.show(state=["w"])

            # 紐⑺몴?⑤룄 ?꾨떖 ?湲?
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
                if time.time() - wait_start > 60:  # 理쒕? 60珥??湲?
                    break
                pygame.time.wait(200)

            dt_row = [self.mouseid, self.day, self.trainingstep, poke_count,
                      time.time() - self.start_time, "TempReached" if reached_target else "TempTimeout", curr_temp, new_target, cue_type, current_state]
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            # ?⑤룄 ?꾨떖 ??30珥덇컙 attenuation 鍮꾪솢?깊솕 ?좎? (鍮꾨툝濡쒗궧)
            att_resume_time = time.time() + 30
            print(f"Attenuation hold: resumes in 30s")

        # ?몄뀡 醫낅즺
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

        Each poke: (1) 蹂댁긽 ?????붾㈃ ON ???쇳꽣 ?ы겕 遺덇?(?湲?猷⑦봽 諛?,
        (2) drift OFF + 紐낅졊 紐⑺몴 ±bump_deg,
        (3) ?ㅼ륫??踰뷀봽 紐⑺몴 ?꾨떖 ?????붾㈃ OFF ???ы겕 媛??
        (4) attenuation_rate (°C/s) ?쒕━?꾪듃 ON.

        white_sec ?몄옄???섏쐞 ?명솚??誘몄궗??. ???붾㈃ 湲몄씠??踰뷀봽 紐⑺몴 ?꾨떖源뚯?.
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
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    choice_tuple[2] = 0
                pygame.time.wait(SENSOR_POLL_WAIT_MS)

            if session_done or not center_poked:
                break

            self.reward.give(0.1)

            curr_temp, target_temp = self._get_shared_temperatures()
            if curr_temp is None:
                curr_temp = target_temp if target_temp is not None else start_temp
            # 踰뷀봽??諛섎뱶??"?꾩옱 紐낅졊 紐⑺몴" 湲곗?(?쒕━?꾪듃 以?紐⑺몴媛 梨붾쾭蹂대떎 ?욎꽌 ?덉쓣 ??
            # ?ㅼ륫留??곕㈃ ???ы겕???섏떗 °C 紐⑺몴 ?먰봽媛 ?????덉쓬 ??batch6 TRL1 濡쒓렇?먯꽌 ?뺤씤??
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
                f"cmd_ref={ref:.3f}°C, avg_now={curr_temp:.3f}°C -> bump_target={new_target:.3f}°C"
            )

            # 踰뷀봽 ?쒖옉: ???붾㈃ + ??援ш컙?먯꽌???ы겕 ?湲?猷⑦봽???놁뼱 ?곗냽 ?ы겕 遺덇?
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

            # 踰뷀봽 紐⑺몴 ?꾨떖(?먮뒗 ??꾩븘?? ?????붾㈃ 醫낅즺 ???ㅼ떆 ?ы겕 媛??
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
        TRL2: 諛⑺뼢 ?숈뒿. left = cold choice(媛먯뇿 ?됯컖), right = hot choice(媛먯뇿 媛??.
        ???쒖젏?먮뒗 ?쒖そ留????쒖떆(hot?봠old 踰덇컝??. 鍮꾪솢??援щ찉 poke??臾댁떆.
        ?щ컮瑜?poke ???고솕硫?+ 媛먯뇿 ?뺤? ??±bump_deg ???고솕硫??댁젣 ??
        ?ㅼ쓬 ?먮줈 ?꾪솚?섍퀬, hot ?꾨즺 ?꾩뿉??+rate ?됯컖 ??醫?, cold ?꾨즺 ?꾩뿉??-rate 媛??????.
        """
        print("=== TRL2: Side alternating hot/cold cue ===")

        file_name_td = self.file_trialdata + "_trial-wise.csv"
        directory = os.path.dirname(file_name_td)
        col_name_td = ['mouseID', 'Day', 'Task', 'Trial', 'Time', 'Event',
                         'Current_Temp', 'Target_Temp', 'ActiveCue', 'PokePos', 'AttSign']

        self.peltier_queue.put(("SET_TEMP", start_temp))
        self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))

        # 'hot' = right留??좏슚, 'cold' = left留??좏슚. 泥??쒖떆??hot(??.
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
                            pygame.time.wait(SENSOR_POLL_WAIT_MS)
                else:
                    if s[1] == 1 and prev_s[1] == 0:
                        correct = True
                        poke_pos = 'left'
                        trial += 1
                        poke_t = time.time() - self.start_time
                        while self.sensor.get()[1] == 1:
                            pygame.time.wait(SENSOR_POLL_WAIT_MS)
                prev_s = s
                pygame.time.wait(SENSOR_POLL_WAIT_MS)

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
                f"[TRL2] trial {trial} ({active_cue}): ref={ref:.3f}°C -> bump_target={new_target:.3f}°C, "
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
        TRL3: TRL_main怨??숈씪??踰뷀봽쨌媛먯뇿(att) 濡쒖쭅.
        留?trial留덈떎 P(both)=(1-single_cue_probability)濡??묒そ ??TRL_main),
        P(single)=single_cue_probability濡??쒖そ留???
        single-cue: trial ?쒖옉 ?쒖젏 att_sign(?쒕━?꾪듃 遺?? hot=+1 / cold=-1)???곕씪
        媛먯뇿 諛⑺뼢??諛섎?履??먮쭔 ?쒖떆(att off(0)?대㈃ hot ??.
        ?묒そ ??both)???곗냽 max_consecutive_both?뚭퉴吏留??덉슜(湲곕낯 3 ????踰덉㎏ ?곗냽 both??湲덉?).
        """
        print("=== TRL3: mixed single-cue / both-cue (att off until bump; then directional drift) ===")
        _both_nominal_pct = (1.0 - single_cue_probability) * 100.0
        print(
            f"[TRL3] nominal both-cue ??{_both_nominal_pct:.1f}% / single-cue ??"
            f"{single_cue_probability * 100.0:.1f}% (?곗냽 both ?곹븳?쇰줈 ?ㅼ젣 鍮꾩쑉? ?ㅻ? ???덉쓬)"
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
                            pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    elif s[1] == 1 and prev_s[1] == 0:
                        choice = 'cold'
                        poke_pos = 'left'
                        trial += 1
                        poke_t = time.time() - self.start_time
                        while self.sensor.get()[1] == 1:
                            pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    prev_s = s
                    pygame.time.wait(SENSOR_POLL_WAIT_MS)
            else:
                # trial ?쒖옉 ?쒖젏 媛먯뇿(?쒕━?꾪듃) 諛⑺뼢怨?諛섎? ?먮쭔 ?쒖떆
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
                                pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    else:
                        if s[1] == 1 and prev_s[1] == 0:
                            correct = True
                            choice = 'cold'
                            poke_pos = 'left'
                            trial += 1
                            poke_t = time.time() - self.start_time
                            while self.sensor.get()[1] == 1:
                                pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    prev_s = s
                    pygame.time.wait(SENSOR_POLL_WAIT_MS)

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
                f"ref={ref:.3f}°C -> bump_target={new_target:.3f}°C (clamped to [{temp_min}, {temp_max}])"
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
        TRL_main: TRL2? ?숈씪?섍쾶 ?쒖옉 ??媛먯뇿 0, 醫?cold쨌??hot ?숈떆 ?쒖떆(??긽).
        踰뷀봽 紐⑺몴: ref±bump_deg ??[temp_min, temp_max]濡??대옩??hot ?곹븳쨌cold ?섑븳 ?숈씪 洹쒖튃).
        踰뷀봽 ??媛먯뇿 諛⑺뼢: choice_sign( hot=+1, cold=-1 )? att_sign??媛숈쑝硫??좎?, ?ㅻⅤ硫?att_sign=choice_sign.
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

        # 0 = 媛먯뇿 ??TRL2 珥덇린? ?숈씪), +1 = hot drift, -1 = cold drift
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
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                elif s[1] == 1 and prev_s[1] == 0:
                    choice = 'cold'
                    poke_pos = 'left'
                    trial += 1
                    poke_t = time.time() - self.start_time
                    while self.sensor.get()[1] == 1:
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                prev_s = s
                pygame.time.wait(SENSOR_POLL_WAIT_MS)

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
                f"ref={ref:.3f}°C -> bump_target={new_target:.3f}°C (clamped to [{temp_min}, {temp_max}])"
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
    # Temperature Lift (TL) - ?좏뻾 ?숈뒿 ?④퀎
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
        TL 怨듯넻 肄붿뼱. Left-only, trial-based.
        - ??trial: choice window(湲곕낯 20s) + feedback window(湲곕낯 40s).
        - Continuous drift is disabled; the setpoint is held during the choice window.
        - choice window ?숈븞 left poke(sensor[1]) 諛쒖깮 ??利됱떆 feedback ?쒖옉.
          誘몃컻????no choice濡?湲곕줉 ??feedback ?쒖옉. left ??poke??臾댁떆.
        - choice??寃쎌슦 feedback window ?숈븞 drift ?뺤? + (choice ?쒖젏 痢≪젙 avg + bump)濡?SET_TEMP,
          ?앷퉴吏 ?좎?. no choice硫?feedback ?쒖옉 ??balanced random drop???곸슜.
        - choice ?곸듅??bump??20-trial balanced random bag?먯꽌 ?좏깮.
        - sound cue = reward 諛몃툕 ?뚮━: trial ?쒖옉 + left poke ??reward.give.
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

            # --- Trial ?쒖옉: target hold + sound cue(諛몃툕 ?뚮━) ---
            self.peltier_queue.put(("SET_ATTENUATION_DIRECT", (0.0, temp_min, temp_max)))
            self.reward.give(trial_start_reward)
            curr_temp, target_temp = self._get_shared_temperatures()
            print(f"\n[TL] --- Trial {trial} start (no drift) ---")
            dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                      time.time() - self.start_time, "TrialStart",
                      curr_temp, target_temp, 'n', 'n', 'n', 'n', 'n']
            self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

            # --- Choice window: left cue blink + left poke 寃異?---
            cw_start = time.time()
            prev_left = self.sensor.get()[1]
            choice = False
            poke_t = None
            blink_on = None  # None?대㈃ 泥?吏꾩엯 ??媛뺤젣 ON
            last_blink = cw_start
            while True:
                now = time.time()
                if (now - start_Ex) >= task_time * 60:
                    session_done = True
                    break
                if (now - cw_start) >= choice_window:
                    break

                # cue blink (~blink_period 留덈떎 ?좉?)
                if blink_on is None or (now - last_blink) >= blink_period:
                    blink_on = True if blink_on is None else (not blink_on)
                    last_blink = now
                    if blink_on:
                        self.screen.display_temp_cue("cold", bottom_gap_fraction=0.2)
                    else:
                        self.screen.show()

                s = self.sensor.get()
                if s[1] == 1 and prev_left == 0:
                    choice = True
                    poke_t = now - self.start_time
                    break
                prev_left = s[1]
                pygame.time.wait(SENSOR_POLL_WAIT_MS)

            # --- Choice 泥섎━ ---
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
                      f"-> target {new_target:.3f}°C (RT={rt}s)")
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

            # --- Feedback window: poke 臾댁떆, 怨좎젙 ??대㉧ ---
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

    def TL1(self, choice_window=20.0):
        print(f"=== TL1: Temperature lift (+5/-5 fixed, choice window {choice_window:g}s) ===")
        self._run_temperature_lift(
            bump_choices=(5.0,),
            no_choice_drop_choices=(5.0,),
            choice_window=choice_window,
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
        - Center poke ???꾩옱 ?⑤룄???곕Ⅸ ?뺣떟痢?side??cue ?쒖떆
        - ?ㅻ떟 side poke??臾댁떆, 120珥?timeout ??cue ?쒓굅 ??泥섏쓬?쇰줈
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
        self.screen.show()  # 鍮??붾㈃

        dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                  time.time() - self.start_time, "SessionStart", curr_temp, target_temp, 'n', 'n', current_state, 'n']
        self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)

        print(f"State: {current_state}")

        att_resume_time = 0.0  # attenuation ?ы솢?깊솕 ?덉빟 ?쒓컖 (0.0 = 利됱떆 媛??

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

            # Center poke ?湲?
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
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    choice_tuple[2] = 0
                if not attenuation_active and time.time() >= att_resume_time:
                    self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                    attenuation_active = True
                pygame.time.wait(SENSOR_POLL_WAIT_MS)

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

            # Side poke ?湲?(timeout: choice_timeout 珥?
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
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    choice_tuple[1] = 0
                elif current_sensor[3] == 1 and choice_tuple[3] == 0:
                    choice_tuple[3] = 1
                    poked_side = 'r'
                    poke_time = time.time() - self.start_time
                    while self.sensor.get()[3] == 1:
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
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

                pygame.time.wait(SENSOR_POLL_WAIT_MS)

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

            # ?뺣떟 泥섎━
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

            # ?⑤룄 ?꾨떖 ??30珥덇컙 attenuation 鍮꾪솢?깊솕 ?좎? (鍮꾨툝濡쒗궧)
            att_resume_time = time.time() + 30
            print(f"Attenuation hold: resumes in 30s")
            self.screen.show()

        print("=== New Stage 2 Session Ended ===")

    def New_Stage3(self, start_temp=30.0, task_time=60,
                   attenuation_rate=0.07, temp_change=4.0, temp_tolerance=0.5,
                   optimal_ref=30.0, att_min=15.0, att_max=45.0, choice_timeout=120):
        """
        New Protocol Stage 3: 50% ?뺣떟留?cue / 50% ?묒そ cue ?쇳빀
        - ?뺣떟留?trial: NS2? ?숈씪 (?ㅻ떟 臾댁떆, 120珥?timeout)
        - ?묒そ trial: ?????좏슚, ?대뒓 履?poke?대룄 ?⑤룄 蹂??
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

        att_resume_time = 0.0  # attenuation ?ы솢?깊솕 ?덉빟 ?쒓컖 (0.0 = 利됱떆 媛??

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

            # Center poke ?湲?
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
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    choice_tuple[2] = 0
                if not attenuation_active and time.time() >= att_resume_time:
                    self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                    attenuation_active = True
                pygame.time.wait(SENSOR_POLL_WAIT_MS)

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

            # 50% ?뺣쪧濡?trial type 寃곗젙
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

            # Side poke ?湲?
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
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    choice_tuple[1] = 0
                elif current_sensor[3] == 1 and choice_tuple[3] == 0:
                    choice_tuple[3] = 1
                    poked_side = 'r'
                    poke_time = time.time() - self.start_time
                    while self.sensor.get()[3] == 1:
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
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
                    else:  # both: ?묒そ 紐⑤몢 ?좏슚
                        result = 'correct' if poked_side == correct_side else 'wrong_but_valid'
                        poke_pos = poked_side
                        new_target = self._ns_target_for_side(target_temp, poked_side, temp_change)
                        break

                pygame.time.wait(SENSOR_POLL_WAIT_MS)

            if result == 'session_end':
                self.screen.show()
                break

            if result == 'timeout':
                self.screen.show()
                dt_row = [self.mouseid, self.day, self.trainingstep, trial,
                          time.time() - self.start_time, "Timeout", curr_temp, target_temp, cue_type, 'n', current_state, trial_type, 'timeout']
                self.TrialData2CSV2(directory, file_name_td, dt_row, col_name_td)
                continue

            # ?⑤룄 蹂??泥섎━
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

            # ?⑤룄 ?꾨떖 ??30珥덇컙 attenuation 鍮꾪솢?깊솕 ?좎? (鍮꾨툝濡쒗궧)
            att_resume_time = time.time() + 30
            print(f"Attenuation hold: resumes in 30s")
            self.screen.show()

        print("=== New Stage 3 Session Ended ===")

    def New_Stage4(self, start_temp=30.0, task_time=60,
                   attenuation_rate=0.07, temp_change=4.0, temp_tolerance=0.5,
                   optimal_ref=30.0, att_min=15.0, att_max=45.0, choice_timeout=120):
        """
        New Protocol Stage 4: ??긽 ?묒そ cue (Full Both)
        - Center poke -> display_temp_both() -> ?묒そ 紐⑤몢 ?좏슚
        - NS3?먯꽌 'both' trial留?怨좎젙??踰꾩쟾
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

        att_resume_time = 0.0  # attenuation ?ы솢?깊솕 ?덉빟 ?쒓컖 (0.0 = 利됱떆 媛??

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

            # Center poke ?湲?
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
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    choice_tuple[2] = 0
                if not attenuation_active and time.time() >= att_resume_time:
                    self.peltier_queue.put(("SET_ATTENUATION_DIRECT", attenuation_rate * attenuation_sign))
                    attenuation_active = True
                pygame.time.wait(SENSOR_POLL_WAIT_MS)

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

            # Side poke ?湲?
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
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    choice_tuple[1] = 0
                elif current_sensor[3] == 1 and choice_tuple[3] == 0:
                    choice_tuple[3] = 1
                    poked_side = 'r'
                    poke_time = time.time() - self.start_time
                    while self.sensor.get()[3] == 1:
                        pygame.time.wait(SENSOR_POLL_WAIT_MS)
                    choice_tuple[3] = 0

                if poked_side is not None:
                    result = 'correct' if poked_side == correct_side else 'incorrect'
                    poke_pos = poked_side
                    new_target = self._ns_target_for_side(target_temp, poked_side, temp_change)
                    break

                pygame.time.wait(SENSOR_POLL_WAIT_MS)

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

            # ?⑤룄 ?꾨떖 ??30珥덇컙 attenuation 鍮꾪솢?깊솕 ?좎? (鍮꾨툝濡쒗궧)
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
    """Stage 1: ?쒖옉 ?⑤룄 23°C (李④???履쎌뿉???쒖옉)"""
    def task(self):
        self.Training_Stage1(start_temp=20.0)

class Training_Stage1_Hot(Task):
    """Stage 1: ?쒖옉 ?⑤룄 37°C (?④굅??履쎌뿉???쒖옉)"""
    def task(self):
        self.Training_Stage1(start_temp=40.0)

class Training_Stage2_Cold(Task):
    """Stage 2: ?쒖옉 ?⑤룄 25°C (optimal 寃쎄퀎 李④???履?"""
    def task(self):
        self.Training_Stage2(start_temp=20.0)

class Training_Stage2_Hot(Task):
    """Stage 2: ?쒖옉 ?⑤룄 35°C (optimal 寃쎄퀎 ?④굅??履?"""
    def task(self):
        self.Training_Stage2(start_temp=40.0)

class Training_Stage3_Full(Task):
    """Stage 3: Full Task (?쒖옉 ?⑤룄 30°C, optimal 以묒븰)"""
    def task(self):
        self.Training_Stage3(start_temp=30.0)

# ============================================================
# New Protocol Task Classes (NS1 ~ NS4)
# ============================================================

class New_Stage1_Task(Task):
    """New Protocol Stage 1: Center poke ??Temp change (center ?뺣떟 cue)"""
    def task(self):
        self.New_Stage1()

class New_Stage2_Task(Task):
    """New Protocol Stage 2: Center ??Side cue ??Temp change"""
    def task(self):
        self.New_Stage2()

class New_Stage3_Task(Task):
    """New Protocol Stage 3: 50% correct_only / 50% both ?쇳빀"""
    def task(self):
        self.New_Stage3()

class New_Stage4_Task(Task):
    """New Protocol Stage 4: ??긽 ?묒そ cue (Full Both)"""
    def task(self):
        self.New_Stage4()

class TRL1_Task(Task):
    """TRL1: Center-only; bump ??white until chamber reaches bump target ??drift 0.07°C/s."""
    def task(self):
        self.TRL1()

class TRL2_Task(Task):
    """TRL2: 醫?cold, ??hot 怨좎젙; ??踰덉뿉 ?쒖そ留??? 踰덇컝???쒖떆."""
    def task(self):
        self.TRL2()

class TRL3_Task(Task):
    """TRL3: TRL_main怨??숈씪 濡쒖쭅 + ?⑥씪/?묒そ ?뺣쪧 ?쇳빀; single? att 諛⑺뼢 諛섎? ?? both ?곗냽 理쒕? 3??"""

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
    """TRL_main: ??긽 ?묒そ ?? 踰뷀봽쨌媛먯뇿??援?TRL3? ?숈씪."""
    def task(self):
        self.TRL_main()

class TL1_Task(Task):
    """TL1: left-only, configurable choice window, fixed +5/-5 outcome."""

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
        choice_window=20.0,
    ):
        self.choice_window = choice_window
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
        self.TL1(choice_window=self.choice_window)

class TL2_Task(Task):
    """TL2: left-only, choice -> +3/3.5/4°C, no-choice ??-1.5/-2/-2.5°C."""
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

