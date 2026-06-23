
# https://meticulousdev.tistory.com/entry/Python-%ED%95%A8%EC%88%98-%ED%98%B8%EC%B6%9C-%ED%9A%9F%EC%88%98-%EA%B3%84%EC%82%B0-%EC%8B%9C-%EC%95%8C%EC%95%84%EB%91%AC%EC%95%BC%ED%95%98%EB%8A%94-Local-Enclosing-Global-and-Built-in-scopes-LEGB-%EA%B7%9C%EC%B9%99

import csv
import json
from datetime import datetime, timedelta
import time
import serial
import re
import pygame

def write_every_n_miliseconds(file_name, minutes, shared_data: dict, dict_lock, start_time, stop_event):
    f = open(file_name, 'a')
    wr = csv.writer(f)
    wr.writerow([
        "time(s)",
        "target_temp",
        "sensor_temp1",
        "sensor_temp2",
        "average_temp",
        "control_mode",
        "control_rate",
        "predicted_temp",
        "delta_pwm",
        "ref_pwm",
    ])
    
    start_date = datetime.now()
    print('csv writing start', start_date)
    now = time.time() - start_time

    time_end = start_time + minutes * 60
    temp1 = 0
    temp2 = 0
    avg_temp = 0
    target_temp = 0
    control_mode = "LAND_HOLD"
    control_rate = None
    predicted_temp = None
    delta_pwm = 0
    ref_pwm = 0

    while not stop_event.is_set():
        now = time.time() - start_time

        with dict_lock:
            temp1 = shared_data["temp1"]
            temp2 = shared_data["temp2"]
            avg_temp = shared_data["average_temp"]
            target_temp = shared_data["target_temp"]
            control_mode = shared_data["control_mode"]
            control_rate = shared_data["control_rate"]
            predicted_temp = shared_data["predicted_temp"]
            delta_pwm = shared_data["delta_pwm"]
            ref_pwm = shared_data["ref_pwm"]

        wr.writerow([
            now,
            target_temp,
            temp1,
            temp2,
            avg_temp,
            control_mode,
            control_rate,
            predicted_temp,
            delta_pwm,
            ref_pwm,
        ])

        # print("target_temp:", f"{target_temp:.3f}", "/", "curr_temp:", f"{avg_temp:.3f}")
        # print("sensor1:", temp1, "/", "sensor2:", temp2)        

        if now > time_end:
            print('data_export')
            print(datetime.now())
            break

        pygame.time.wait(500)

    f.close()

# def write_every_n_miliseconds(file_name, minutes, shared_data: dict, dict_lock):
#     global pre_time
#     f = open(file_name, 'a')
#     wr = csv.writer(f)
#     csv_start_time = datetime.now()
#     time_end = csv_start_time + timedelta(minutes=minutes, milliseconds=500)
#     print('csv writing start')

#     while True:
#         startTime = round(datetime.utcnow().timestamp() * 1000)
#         if (startTime - pre_time >= 10):
#             cnt_time[0] += 1
#             if (cnt_time[0] == 1):
#                 print(cnt_time, datetime.now())
#             pre_time = startTime
#             now_time = round(time.time(), 3)

#             wr.writerow([*cnt_time, *sensor.get(), now_time])

#         now_ = datetime.now() #꼭 시간이 되어야만 멈출래? 음.. trial 시간이 정해져 있다고 하면 이게 의미 있긴 하지. 일단 뭐 test로 하는 거니까 줄여 놓는 걸로.
#         if now_>next_time:
#             print('data_export')
#             print(now_)
#             break

#     f.close()



# if __name__ == "__main__":
#     df = {
#     'Trial' : [0, 0, 0, 1, 1, 1, 2, 2, 2],
#     'Position' : ['l', 'm', 'r', 'l', 'm', 'r', 'l', 'm', 'r'],
#     'Length' : [0, 200, 0, 0, 200, 0, 0, 200, 0],
#     'Width' : [30, 30, 30, 30, 30, 30, 30, 30, 30],
#     'Thickness' : [None, None, None, None, None, None, None, None, None],
#     'Color' : [(255, 255, 255), (255, 255, 255), (255, 255, 255), (255, 255, 255), (255, 255, 255), (255, 255, 255), (255, 255, 255), (255, 255, 255), (255, 255, 255)],
#     'Poked Site' : [] }
    
#     TrialData2SCV("home/pi/JJH/result/test0001", df)
