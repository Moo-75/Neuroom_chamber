from datetime import datetime
from pytz import timezone
import os
import multiprocessing
from queue import Empty
import data_export
import math
import json
import csv
import pygame
import maze
from task_temp import *
import time

#dir_ = os.getcwd()

def attenuation_func(temp):
    # min temp 10, max temp 40, optimal 30
    if temp >= 10 and temp <= 30:
        return (temp - 10) / 20.0
    elif temp > 30 and temp <= 40:
        return (40 - temp) / 10.0

    return 0.0


def is_valid_temperature(value):
    return value is not None and math.isfinite(value)

def sensor_worker(start_time, sensor_file_name, stop_event, json_dir):
    sensor = maze.Sensor(json_dir)

    f = open(sensor_file_name, 'a')
    wr = csv.writer(f)
    wr.writerow(["time(s)", "sensor_reward", "sensor_left", "sensor_center", "sensor_right"])

    while not stop_event.is_set():
        wr.writerow([time.time() - start_time, *sensor.get()])
        pygame.time.wait(10)

    return


# drift 적분에 쓰는 dt 상한(초). OS 절전/스케줄링으로 루프가 오래 멈추면
# attenuation * dt 한 번에 수 °C씩 튀는 것을 막음.
_MAX_ATTENUATION_DT_SEC = 0.5

# 한 루프에서 꺼내 처리할 큐 명령 상한(폭주 방지)
_MAX_QUEUE_DRAIN = 48


def _apply_peltier_queue_item(peltier, command, value, shared_data, dict_lock, result_queue):
    """단일 (command, value) 처리. 'stop'이면 워커 종료 요청."""
    if command == 'SET_TEMP':
        peltier.set_target_temperature(value)

    elif command == 'SET_ON_WAIT':
        peltier.set_target_temperature(value)
        peltier.start_control()
        result_queue.put(
            peltier.temperature_seton(
                value, shared_data=shared_data, dict_lock=dict_lock
            )
        )

    elif command == 'SET_ATTENUATION':
        peltier.set_temperature_attenuation(value)
        peltier.use_attenuation_func = True

    elif command == 'SET_ATTENUATION_DIRECT':
        if isinstance(value, tuple) and len(value) == 3:
            rate, att_min, att_max = value
            peltier.set_temperature_attenuation(rate)
            peltier.att_min = att_min
            peltier.att_max = att_max
        else:
            peltier.set_temperature_attenuation(value)
            peltier.att_min = 15.0
            peltier.att_max = 45.0
        peltier.use_attenuation_func = False

    elif command == 'TEMP_UPDOWN':
        peltier.temp_updown(value)

    elif command == 'STOP':
        peltier.stop_control()
        return 'stop'

    return None


def peltier_worker(command_queue, result_queue, shared_data, dict_lock, stop_event):
    """펠티어 모듈을 전담해서 관리할 프로세스 함수."""
    peltier = maze.Peltier_module()
    peltier.use_attenuation_func = True  # 기본값: attenuation_func 사용
    peltier.start_control()              # ← 추가: is_running=true 보장 (리셋 동작에 의존 X)
    pygame.time.wait(100)
    prev_time = time.time()
    command = None

    while not stop_event.is_set():
        try:
            # 1. 명령: 한 루프에 쌓인 큐를 최대한 비워 연속 SET_TEMP+ATTENUATION 지연을 줄임
            batch = []
            while len(batch) < _MAX_QUEUE_DRAIN:
                try:
                    batch.append(command_queue.get_nowait())
                except Empty:
                    break
            command = batch[-1][0] if batch else None
            for cmd_item in batch:
                command, value = cmd_item
                if _apply_peltier_queue_item(
                    peltier, command, value, shared_data, dict_lock, result_queue
                ) == 'stop':
                    peltier.close()
                    return

            # 2. 온도 읽기: 매 루프 갱신해 shared target_temp와 시간축 맞춤 (이전: 3루프마다)
            temp1, temp2 = peltier.get_temperatures()
            temps_valid = is_valid_temperature(temp1) and is_valid_temperature(temp2)
            avg_temp = (temp1 + temp2) / 2 if temps_valid else None
            with dict_lock:
                shared_data['temp1'] = temp1 if temps_valid else None
                shared_data['temp2'] = temp2 if temps_valid else None
                shared_data['average_temp'] = avg_temp

            # 3. Attenuation 계산
            target_temperature = peltier.target_temp
            elapsed_since_prev = time.time() - prev_time
            if elapsed_since_prev < 0:
                elapsed_since_prev = 0.0
            elapsed_since_prev = min(elapsed_since_prev, _MAX_ATTENUATION_DT_SEC)
            if peltier.use_attenuation_func:
                attenuation_delta = attenuation_func(target_temperature) * peltier.attenuation * elapsed_since_prev
            else:
                attenuation_delta = peltier.attenuation * elapsed_since_prev

            new_target = target_temperature + attenuation_delta

            if not peltier.use_attenuation_func:
                att_min = getattr(peltier, 'att_min', 15.0)
                att_max = getattr(peltier, 'att_max', 45.0)
                new_target = max(att_min, min(new_target, att_max))

            if abs(new_target - target_temperature) > 1e-4:
                peltier.set_target_temperature(new_target)

            with dict_lock:
                shared_data['target_temp'] = peltier.target_temp

            prev_time = time.time()
            pygame.time.wait(100)

        except Exception as e:
            print(command)
            print(f"[Peltier Process] Error: {e}")

    peltier.close()

def check_starting(experiment_start, csv_write_dir, mouse_id, session):
    if experiment_start not in ('y', 'Y'):
        raise ValueError(f"Experiment not confirmed (got '{experiment_start}'). Enter 'y' to start.")
    experiment_time = datetime.now(timezone('Asia/Seoul'))
    experiment_time_str = experiment_time.strftime("%Y-%m-%d_%H-%M-%S")
    Video_file_name        = os.path.join(csv_write_dir, f"Video_{mouse_id}_{session}_{experiment_time_str}.avi")
    TrialData_file_name    = os.path.join(csv_write_dir, f"TD_{mouse_id}_{session}_{experiment_time_str}")
    Temperature_file_name  = os.path.join(csv_write_dir, f"Temperature_{mouse_id}_{session}_{experiment_time_str}.csv")
    FrameTime_file_name    = os.path.join(csv_write_dir, f"FrameTime_{mouse_id}_{session}_{experiment_time_str}.csv")
    SensorTime_file_name   = os.path.join(csv_write_dir, f"SensorTime_{mouse_id}_{session}_{experiment_time_str}.csv")
    return Temperature_file_name, Video_file_name, FrameTime_file_name, TrialData_file_name, SensorTime_file_name
    
def create_path(csv_write_dir, file_name):
    os.makedirs(csv_write_dir, exist_ok=True)
    open(file_name, 'w').close()
    return file_name

if __name__ == '__main__':
    json_dir = input("Please enter your json file :")
    
    with open (json_dir, 'r') as json_:
        data = json.load(json_)
    
    while True:
        protocol = input("Please type protocol (e.g. OBT, TBT): ")
        protocol = "../" + protocol 
        break

    mouse_id = input('Please enter the mouse id : ')
    session = input('Please enter the session (ex. d18_p1) : ')

    while mouse_id == '' or session == '':
        mouse_id = input('Please reenter the mouse id (do not use _ in the name) : ')
        session = input('Please reenter the session : ')
    
    temperature_seton = input('If do you need temperature set on, press y  ')
    need_seton = False
    initial_target_temp = None

    if temperature_seton == "y" or temperature_seton == "Y":
        target_temp = input('Please enter the target temperature : ')
        while True:
            try:
                target_temp = float(target_temp)
                need_seton = True
                initial_target_temp = target_temp
                break
            except:
                target_temp = input('Please reenter the target temperature : ')

    multi_var_manager = multiprocessing.Manager()

    shared_data = multi_var_manager.dict({
        "temp1" : 0.0,
        "temp2" : 0.0,
        "average_temp" : 0.0,
        "target_temp" : 0.0,
        "control_mode" : "LAND_HOLD",
        "control_rate" : None,
        "predicted_temp" : None,
        "delta_pwm" : 0,
        "ref_pwm" : 0,
        "initial_target_temp" : initial_target_temp,
    })
    dict_lock = multiprocessing.Lock()

    stop_event = multi_var_manager.Event()

    peltier_queue = multiprocessing.Queue()
    result_queue = multiprocessing.Queue()
    peltier_process = multiprocessing.Process(
        target=peltier_worker,
        args=(peltier_queue, result_queue, shared_data, dict_lock, stop_event),
    )
    peltier_process.start()

    if need_seton:
        peltier_queue.put(("SET_ON_WAIT", target_temp))
        try:
            seton_success = result_queue.get(timeout=300)
        except Empty:
            seton_success = False
        if not seton_success:
            stop_event.set()
            peltier_process.join(timeout=5)
            raise RuntimeError(f"Failed to reach target temperature {target_temp}.")
    while True:
        experiment_start = input('If the experiment start, press y  ')
        print(mouse_id, session)
        print(experiment_start)
        csv_write_dir = os.path.join(protocol, mouse_id + "_" + session)
        try:
            Temperature_file_name, Video_file_name, FrameTime_file_name, TrialData_file_name, SensorTime_file_name = check_starting(experiment_start, csv_write_dir, mouse_id, session)
            break
        except ValueError as e:
            print(e)
    SensorTime_file_name = create_path(csv_write_dir, SensorTime_file_name)
        
    while (1):
        task = input("""
To start enter number you want to run
    [w] reward hole test
    [p1] Pretraining 1 
    [p22] Pretraining 2 
    [p32] Pretraining 3 
    [p42]
    [p5]
    [ta10]
    [ta45]
    [trl10] Temperature Reversal Learning 10/5
    [trl45] Temperature Reversal Learning 45/5
    [trl101] Temperature Reversal Learning 10/ 1 hour
    [trl451] Temperature Reversal Learning 45/ 1 hour
    [trl152] Temperature Reversal Learning 15/ 1 hour
    [trl402] Temperature Reversal Learning 40/ 1 hour
    [BRL_test] Temperature test
    [BRL_cold_block] BRL Cold to Hot task
    [BRL_cold_block_2] BRL Cold to Hot task
    [BRL_hot_block] BRL Hot to Cold task
    [BRL_hot_block_2] BRL Hot to Cold task
    [BRL_find_optimal_block] BRL Find Optimal task
    [BRL_optimal_block_2] BRL Find Optimal task
    [BRL_cold_no_block] BRL Cold to Hot task
    [BRL_hot_no_block] BRL Hot to Cold task
    [BRL_optimal_no_block] BRL Find Optimal task
    [BRL_preference_cold] BRL Preference test, cold to hot task
    [BRL_preference_hot] BRL Preference test, hot to cold task
    
    [POA] POA task

    === Training Protocol (Old) ===
    [TS1_cold] Training Stage 1 (start 23°C)
    [TS1_hot] Training Stage 1 (start 37°C)
    [TS2_cold] Training Stage 2 (start 25°C)
    [TS2_hot] Training Stage 2 (start 35°C)
    [TS3] Training Stage 3 Full Task

    === New Protocol ===
    [NS1] New Stage 1: Center poke -> Temp change
    [NS2] New Stage 2: Center -> Side cue -> Temp change
    [NS3] New Stage 3: 50% correct / 50% both
    [NS4] New Stage 4: Always both cue
    [TRL1] TRL1: 3°C bump, white until bump temp reached → 0.07°C/s drift
    [TRL2] TRL2: 좌 cold / 우 hot 번갈 큐, 맞는 쪽 poke만 유효
    [TRL3] TRL3: both 비율은 실행 시 입력; single은 감쇠 방향 반대 큐 — 범프·감쇠는 TRL_main과 동일
    [TRL_main] TRL_main: 항상 양쪽 큐 동시; hot=+bump+드리프트반전, cold=-bump+드리프트유지

    === Temperature Lift (선행 학습) ===
    [TL1] TL1: left-only, choice +5°C, no-choice -5°C (20s choice / 40s feedback)
    [TL2] TL2: left-only, choice +3/3.5/4°C, no-choice -1.5/-2/-2.5°C (10s choice / 20s feedback)

    [0] Exit
""")
        
        start_time = time.time()
        sensor_process = multiprocessing.Process(target=sensor_worker, args = (start_time, SensorTime_file_name, stop_event, json_dir))
        sensor_process.start()

        if task == "0":
            print("Selected Exit")
            break
        elif task == "p1":
            instance = Pretraining_1(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "p22":
            instance = Pretraining_22(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "p32":
            instance = Pretraining_32(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "p42":
            instance = Pretraining_42(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "p5":
            instance = Pretraining_5(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break

        elif task == "ta10":
            instance = TAL10(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break

        elif task == "ta45":
            instance = TAL45(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        
        elif task == "trl10":
            instance = TRL_start10(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break

        elif task == "trl45":
            instance = TRL_start45(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "trl101":
            instance = TRL_start10_1(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break

        elif task == "trl451":
            instance = TRL_start45_1(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "trl152":
            instance = TRL_start15_2(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break

        elif task == "trl402":
            instance = TRL_start40_2(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "trl153":
            instance = TRL_start15_3(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break

        elif task == "trl403":
            instance = TRL_start40_3(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "tr10":
            instance = TRL_reward_10(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "tr25":
            instance = TRL_reward_25(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "tr45":
            instance = TRL_reward_45(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (SensorTime_file_name, data["min"]))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_test":
            instance = Temperature_test(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_cold_block":
            instance = Cold_to_hot_block(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_hot_block":
            instance = Hot_to_cold_block(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue,stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_cold_block_2":
            instance = Cold_to_hot_block_2(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_hot_block_2":
            instance = Hot_to_cold_block_2(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_find_optimal_block":
            instance = Find_optimal_block(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue,stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_preference_hot":
            instance = Preference_test_hot(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue,stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_preference_cold":
            instance = Preference_test_cold(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue,stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_cold_no_block":
            instance = Cold_to_hot_no_block(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_hot_no_block":
            instance = Hot_to_cold_no_block(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "BRL_optimal_no_block":
            instance = Find_optimal_block(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time))
            task_.start()
            instance.run()
            task_.join()
            break
        
        elif task == "POA":
            instance = POA_task(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue,stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        
        # ===== Training Protocol =====
        elif task == "TS1_cold":
            instance = Training_Stage1_Cold(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "TS1_hot":
            instance = Training_Stage1_Hot(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "TS2_cold":
            instance = Training_Stage2_Cold(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "TS2_hot":
            instance = Training_Stage2_Hot(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "TS3":
            instance = Training_Stage3_Full(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break

        # ===== New Protocol =====
        elif task == "NS1":
            instance = New_Stage1_Task(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "NS2":
            instance = New_Stage2_Task(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "NS3":
            instance = New_Stage3_Task(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "NS4":
            instance = New_Stage4_Task(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "TRL1":
            instance = TRL1_Task(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "TRL2":
            instance = TRL2_Task(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "TRL3":
            while True:
                raw = input(
                    "TRL3: 양쪽 큐(both cue) trial 비율(%)을 입력하세요 (0~100, Enter=50): "
                ).strip()
                if raw == "":
                    both_pct = 50.0
                    break
                try:
                    both_pct = float(raw.replace(",", "."))
                except ValueError:
                    print("숫자를 입력해 주세요.")
                    continue
                if 0.0 <= both_pct <= 100.0:
                    break
                print("0 이상 100 이하로 입력해 주세요.")
            print(f"[TRL3] both-cue ≈ {both_pct:g}% / single-cue ≈ {100.0 - both_pct:g}% 로 시작합니다.")
            instance = TRL3_Task(
                json_dir,
                Video_file_name,
                FrameTime_file_name,
                TrialData_file_name,
                mouse_id,
                session,
                shared_data,
                dict_lock,
                start_time,
                peltier_queue,
                stop_event,
                both_cue_percent=both_pct,
            )
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "TRL_main":
            instance = TRL_main_Task(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "TL1":
            instance = TL1_Task(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        elif task == "TL2":
            instance = TL2_Task(json_dir, Video_file_name, FrameTime_file_name, TrialData_file_name, mouse_id, session, shared_data, dict_lock, start_time, peltier_queue, stop_event)
            task_ = multiprocessing.Process(target=data_export.write_every_n_miliseconds, args = (Temperature_file_name, data["min"], shared_data, dict_lock, start_time, stop_event))
            task_.start()
            instance.run()
            task_.join()
            break
        else:
            print("Wrong input, please try again\n")

    stop_event.set()
    print("Proccess made an end\nShutting down...\n")
    sys.exit()
