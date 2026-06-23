import json
from typing import Set
import math
import pygame
import os
import RPi.GPIO as GPIO #general purpose Input/Output
import time
import random
import serial

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

df = {'Trial':[], 'Position':[],'Length':[], 
        'Width':[],'Thickness':[],'Color':[], 'Poked Site':[]}

class NoneChoiceError(Exception):
    pass
class NoDataFrame(Exception):
    pass

class Display:
    def __init__(self, dir):
        # load file
        with open (dir, "r") as config:
            data = json.load(config)
            self.screen = pygame.display.set_mode((data['display']['width'],data['display']['height']), pygame.NOFRAME)
            self.screen.fill((0, 0, 0))
            pygame.mouse.set_visible(False)

        # 이미지 사전 로드 (json 파일 기준 디렉토리에서 절대경로로 로드)
        img_dir = os.path.dirname(os.path.abspath(dir))
        self._img_hot = pygame.transform.scale(
            pygame.image.load(os.path.join(img_dir, 'Hot.png')), (200, 200))
        self._img_cold = pygame.transform.scale(
            pygame.image.load(os.path.join(img_dir, 'Cold.png')), (200, 200))
        self._img_center = pygame.transform.scale(
            pygame.image.load(os.path.join(img_dir, 'Center.png')), (200, 200))

    
    def show(self, state = []):
        if len(state) == 0:
            self.screen.fill((0, 0, 0))
            pygame.display.update()
        else:
            for i in state:
                # if i =='l':
                #     self.screen.blit(self.img, (35, 200))
                # elif i =='r':
                #     self.screen.blit(self.img, (575, 200))
                # elif i =='m':
                #     self.screen.blit(self.img, (305, 200))
                if i =="w":
                    self.screen.fill((255, 255, 255))
                elif i =='g':
                    self.screen.fill((128,128,128))
                elif i == "dg":
                    self.screen.fill((64,64,64))
                # Outcome phase 전용 색상
                elif i == "warm":  # 온도 상승 - 따뜻한 빨간 계열
                    self.screen.fill((255, 100, 100))
                elif i == "cool":  # 온도 하강 - 차가운 파란 계열
                    self.screen.fill((100, 100, 255))
            pygame.display.flip()

    def draw_square(self, position, color, side, base=50):
        # Width of one third of the screen
        third_width = self.screen.get_width() // 3
        
        # Determine the x position based on the specified position
        if position == 'left':
            x = (third_width - side) // 2  # Center in the first third
        elif position == 'center':
            x = (self.screen.get_width() - side) // 2  # Center in the middle
        else:  # 'right'
            x = 2 * third_width + (third_width - side) // 2  # Center in the last third
        
        # Position the square 50 pixels above the bottom of the screen
        y = self.screen.get_height() - side - base
        
        # Drawing the square
        pygame.draw.rect(self.screen, color, (x, y, side, side))
        
        return pygame.Rect(x, y, side, side)  # Return the rect for updating

    def draw_bar(self, length, position, width=150, thickness=None, color=(255, 255, 255), base= 450):
        '''
        This function draws a bar among left, middle, and right
        with input length, width, and type

        Args:
            screen: screen instance after basic setting 
            length(int, float): length of the bar
            position(str): choose to write among "l", "m", and "r",
                otherwise do not display anything
            width(int, float): width of the bar, default is 30
            thickness: thickness of the boundary of the bar.
                default setting is None.
            color(tuple, int): RGB color, default setting is white
        '''
        base = base
        rect = None  # This will store the rectangle to update

        if position == "l" and length != 0:
            rect = pygame.Rect(135-width/2, base-length, width, length)
        elif position == "m" and length != 0:
            rect = pygame.Rect(400-width/2, base-length, width, length)
        elif position == "r" and length != 0:
            rect = pygame.Rect(665-width/2, base-length, width, length)

        if rect is not None:
            if thickness is None:
                pygame.draw.rect(self.screen, color, rect)
            else:
                pygame.draw.rect(self.screen, color, rect, thickness)
            pygame.display.update(rect)

    def draw_rect(self, position, height=200, width=200,
                thickness=None, color=(255, 255, 255), base = 320):
        '''
        This function draws a bar among left, middle, and right
        with input length, width, and type

        Args:
            screen: screen instance after basic setting 
            length(int, float): length of the bar
            position(str): choose to write amoung "l", "m", and "r",
                otherwise do not display anything
            width(int, float): width of the bar, default is 30
            thickness: thickness of the boundary of the bar.
                default setting is None.
            color(tuple, int): RGB color, default setting is white
        '''
        base = base #470
        if thickness is None:
            if position == "l" and height != 0:
                pygame.draw.rect(self.screen, color,
                            pygame.Rect(135-width/2, base-height, width, height))
                
            elif position == "m" and height != 0:
                pygame.draw.rect(self.screen, color,
                                pygame.Rect(400-width/2, base-height, width, height))
                
            elif position == "r" and height != 0:
                pygame.draw.rect(self.screen, color,
                                pygame.Rect(665-width/2, base-height, width, height))
                
            else:
                pass
        else:
            if position == "l" and height != 0:
                pygame.draw.rect(self.screen, color,
                                pygame.Rect(150-width/2, base-height, width, height),
                                thickness)
                
            elif position == "m" and height != 0:
                pygame.draw.rect(self.screen, color,
                                pygame.Rect(400-width/2, base-height, width, height),
                                thickness)
                
            elif position == "r" and height != 0:
                pygame.draw.rect(self.screen, color,
                                pygame.Rect(650-width/2, base-height, width, height),
                                thickness)
                
            else:
                pass
        pygame.display.flip()
    
    def wrong_screen(self, side=150, p = (400, 400)):
        p1 = p
        p2 = (p[0] - side/2, p[1] + side * 3**0.5 /2)
        p3 = (p[0] + side/2, p[1] + side * 3**0.5 /2)

        pygame.draw.polygon(self.screen, (255, 255, 0), [p1, p2, p3])
        pygame.display.update()

    


    def display_bars(self, len_ls, width_ls, th_ls=[None, None, None],
         c_ls=[(255, 255, 255), (255, 255, 255), (255, 255, 255)]):
        '''
        This function can display multiple bars with different setting.

        Args:
            N_trial: number of trial
            len_ls: (list,3)list of length
            width_ls: (list,3)list of width, default is 30
            th_ls: (list,3)list of thickness, default is None
            c_ls: (list,3)list of color code, default is (255, 255, 255)  

        Return:
            rows        
        '''
        
        posit_code = ["l", "m", "r"]

        for i in range(3):
            self.draw_bar(len_ls[i], posit_code[i], width= width_ls[i],
                thickness=th_ls[i], color=c_ls[i]) #self.screen을 맨 앞 인수로 추가하는 실수 동일하게 발생
            # Add new element to the list of each value of df
            
        pygame.display.update()
        
    def display_temp_cue(self, temp):
        # Calculate positions
        third_width = 800 // 3
        cold_x = (third_width - 200) // 2
        hot_x = 2 * third_width + (third_width - 200) // 2
        image_y = (480 - 200) // 2  # Vertically center

        self.screen.fill((0, 0, 0))
        if temp == "hot":
            self.screen.blit(self._img_hot, (hot_x, image_y))
        elif temp == "cold":
            self.screen.blit(self._img_cold, (cold_x, image_y))
        pygame.display.flip()

    def display_temp_both(self):
        # Calculate positions
        third_width = 800 // 3
        cold_x = (third_width - 200) // 2
        hot_x = 2 * third_width + (third_width - 200) // 2
        image_y = (480 - 200) // 2  # Vertically center

        self.screen.fill((0, 0, 0))
        self.screen.blit(self._img_hot, (hot_x, image_y))
        self.screen.blit(self._img_cold, (cold_x, image_y))
        pygame.display.flip()

    def display_temp_cue_center(self, temp):
        """Center(중앙 1/3 영역)에 hot 혹은 cold cue를 표시. New Protocol Stage 1용."""
        # Center 1/3 영역 중앙: x = 800/2 - 100 = 300
        center_x = (800 - 200) // 2  # 300
        image_y = (480 - 200) // 2   # 140

        self.screen.fill((0, 0, 0))  # 화면 클리어
        if temp == "hot":
            self.screen.blit(self._img_hot, (center_x, image_y))
        elif temp == "cold":
            self.screen.blit(self._img_cold, (center_x, image_y))
        pygame.display.flip()

    def display_start_cue_center(self):
        center_x = (800 - 200) // 2
        image_y = (480 - 200) // 2

        self.screen.fill((0, 0, 0))
        self.screen.blit(self._img_center, (center_x, image_y))
        pygame.display.flip()

    def erase_temp_cue(self, temp):
         # Calculate positions (same as in display_temp_cue)
        third_width = 800 // 3
        cold_x = (third_width - 200) // 2
        hot_x = 2 * third_width + (third_width - 200) // 2
        image_y = (480 - 200) // 2

        # Create a rectangle to cover the image
        cover_rect = pygame.Rect(0, 0, 200, 200)

        if temp == "hot":
            cover_rect.topleft = (hot_x, image_y)
        elif temp == "cold":
            cover_rect.topleft = (cold_x, image_y)

        # Fill the rectangle with the background color
        # Assuming the background is white, change as needed
        self.screen.fill((0, 0, 0), cover_rect)
        
        # Update the display
        pygame.display.update(cover_rect)
        
            
    
class Sensor:
    '''
    This class assgins pin positions for inputs of changed value recognized by sensor and controls inputs.
    We can use this class by storing the detected value in some variables and using it in other lines of code.
    '''
    def __init__(self, dir):
        with open (dir, "r") as config:
            data = json.load(config)
            self.reward = data["GPIO"]["nose_poke_reward"]
            self.left = data["GPIO"]["nose_poke_left"]
            self.center = data["GPIO"]["nose_poke_center"]
            self.right = data["GPIO"]["nose_poke_right"]
        GPIO.setup(self.reward, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
        GPIO.setup(self.left, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
        GPIO.setup(self.center, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
        GPIO.setup(self.right, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
    def get(self):
        '''
        This method returns detected value in reward port, and three poking hole: left, center, and right.
        '''
        return (GPIO.input(self.reward), GPIO.input(self.left), GPIO.input(self.center), GPIO.input(self.right))
    



        
class Reward:
    '''
    This class assgins pin positions for outputs and controls outputs such as motor controling the mass of reward and LED.
    '''
    def __init__(self, dir):
        with open (dir, "r") as config:
            self.data = json.load(config)
            GPIO.setup (self.data["GPIO"]["reward_motor"], GPIO.OUT, initial = GPIO.LOW) 
            GPIO.setup (self.data["GPIO"]["reward_led"], GPIO.OUT, initial = GPIO.LOW)
            GPIO.setup (self.data["GPIO"]["wrong_led"], GPIO.OUT, initial = GPIO.LOW)
    def give(self, duration):
        '''
        This method controls the amount of reward by operating the motor for 'duration' 
        
        Args:
            duration
        '''
        RG_time = time.time()
        GPIO.output(self.data["GPIO"]["reward_motor"], GPIO.HIGH)
        while True: 
            if time.time() - RG_time >= duration:
                break
        GPIO.output(self.data["GPIO"]["reward_motor"], GPIO.LOW)
    def light(self, yes): 
        '''
        This method turns on LED when the boolean parameter is True.

        Args:
            yes(Boolean)
        '''
        if yes == True: 
            GPIO.output(self.data["GPIO"]["reward_led"], GPIO.HIGH)
        else:
            GPIO.output(self.data["GPIO"]["reward_led"], GPIO.LOW)

    def wrong(self, yes):
        '''
        This method turns on wrong LED when mouse choose wrong poking port

        Args:
            yes(Boolean)
        '''
        if yes == True: 
            GPIO.output(self.data["GPIO"]["wrong_led"], GPIO.HIGH)
        else:
            GPIO.output(self.data["GPIO"]["wrong_led"], GPIO.LOW)

class Photometry:
    def __init__(self, dir):
        with open (dir, "r") as config:
            self.data = json.load(config)
            self.fp1 = self.data["GPIO"]["BNC1"]
            self.fp2 = self.data["GPIO"]["BNC2"]
            self.fp3 = self.data["GPIO"]["BNC3"]
            self.fp4 = self.data["GPIO"]["BNC4"]
        GPIO.setup(self.fp1, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(self.fp2, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(self.fp3, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(self.fp4, GPIO.OUT, initial=GPIO.LOW)

    def FP1_on(self, on=False):
        if on == False:
            fp_time = round(time.time(), 3)
            return fp_time
        else:
            GPIO.output(self.fp1, GPIO.HIGH)
            fp_time = round(time.time(), 3)
            return fp_time
    
    def FP1_off(self, on=False):
        if on == False:
            fp_time = round(time.time(), 3)
            return fp_time
        else:
            GPIO.output(self.fp1, GPIO.LOW)
            fp_time = round(time.time(), 3)
            return fp_time
    
    def FP2_on(self, on=False):
        if on == False:
            fp_time = round(time.time(), 3)
            return fp_time
        else:
            GPIO.output(self.fp2, GPIO.HIGH)
            fp_time = round(time.time(), 3)
            return fp_time

    def FP2_off(self, on=False):
        if on == False:
            fp_time = round(time.time(), 3)
            return fp_time
        else:
            GPIO.output(self.fp2, GPIO.LOW)
            fp_time = round(time.time(), 3)
            return fp_time
    
    def FP3_on(self, on=False):
        if on == False:
            fp_time = round(time.time(), 3)
            return fp_time
        else:
            GPIO.output(self.fp3, GPIO.HIGH)
            fp_time = round(time.time(), 3)
            return fp_time

    def FP3_off(self, on=False):
        if on == False:
            fp_time = round(time.time(), 3)
            return fp_time
        else:
            GPIO.output(self.fp3, GPIO.LOW)
            fp_time = round(time.time(), 3)
            return fp_time
    
    def FP4_on(self, on=False):
        if on == False:
            fp_time = round(time.time(), 3)
            return fp_time
        else:
            GPIO.output(self.fp4, GPIO.HIGH)
            fp_time = round(time.time(), 3)
            return fp_time
    
    def FP4_off(self, on=False):
        if on == False:
            fp_time = round(time.time(), 3)
            return fp_time
        else:
            GPIO.output(self.fp4, GPIO.LOW)
            fp_time = round(time.time(), 3)
            return fp_time

class Peltier_module:
    def __init__(self):
        self.ser = serial.Serial()
        self.ser.port = '/dev/arduino'
        self.ser.baudrate = 115200
        self.ser.timeout = 1
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        self.target_temp = 25.0
        self.attenuation = 0.0

        pygame.time.wait(2000) # 아두이노 기다리기
        initial_message = self.ser.readline().decode('utf-8').strip()
        print(f"Arduino says: {initial_message}")
        if "Ready" not in initial_message:
            print("Warning: Arduino might not be ready.")
        self._last_set_temp_cmd_monotonic = 0.0
        self.set_temp_min_interval_sec = 0.05

    def send_command(self, command, no_response = True):
        """명령을 보내고 아두이노의 첫 번째 응답 라인을 읽어 반환합니다."""
        full_command = f"{command}\n"
        self.ser.write(full_command.encode('utf-8'))

        if no_response: return

        timeout_start = time.time()
        while self.ser.in_waiting == 0:
            if time.time() - timeout_start > 2.0:
                print(f"Warning: Arduino response timeout for command: {command}")
                return None
            pygame.time.wait(50)

        return self.ser.readline().decode('utf-8').strip()

    def get_temperatures(self):
        """현재 온도 값 (센서1, 센서2)를 튜플로 반환합니다."""
        response = self.send_command("GET_TEMP", no_response= False)
        if response:
            try:
                temp1, temp2 = map(float, response.split(','))
                if not (math.isfinite(temp1) and math.isfinite(temp2)):
                    return None, None
                return temp1, temp2
            except (ValueError, IndexError):
                return None, None
        return None, None

    def temperature_seton(self, temp, tolerance=0.5, timeout_sec=295, shared_data=None, dict_lock=None):
        """목표 도달까지 대기. shared_data/dict_lock를 넘기면 이 구간에서도
        multiprocessing 공유 dict가 갱신되어(워커 메인 루프가 멈춰 있어도)
        로그·UI의 target/센서 값이 아두이노 실제 목표와 어긋나지 않게 한다."""
        def _sync_shared(t1, t2, curr):
            if shared_data is None or dict_lock is None:
                return
            with dict_lock:
                if t1 is not None and t2 is not None and curr is not None:
                    shared_data['temp1'] = t1
                    shared_data['temp2'] = t2
                    shared_data['average_temp'] = curr
                shared_data['target_temp'] = self.target_temp

        temp = self.set_target_temperature(temp=temp)
        if shared_data is not None and dict_lock is not None:
            with dict_lock:
                shared_data['target_temp'] = self.target_temp
        print("waiting for temperature set on ...")
        deadline = time.time() + timeout_sec
        while True:
            temp1, temp2 = self.get_temperatures()
            if temp1 is None or temp2 is None:
                if time.time() >= deadline:
                    print(f"temperature set on timeout at target {temp} C")
                    return False
                pygame.time.wait(200)
                continue
            curr_temp = (temp1 + temp2) / 2.0
            _sync_shared(temp1, temp2, curr_temp)
            if curr_temp is not None \
                and curr_temp >= temp - tolerance and curr_temp <= temp + tolerance:
                break
            if time.time() >= deadline:
                print(f"temperature set on timeout at target {temp} C (current: {curr_temp} C)")
                return False
            print(curr_temp,"/", temp1,"/", temp2)
            pygame.time.wait(200)

        print(f"temperature set on {curr_temp} C")
        return True

    def set_target_temperature(self, temp):
        """목표 온도를 설정합니다."""
        temp = float(temp)
        if abs(temp - self.target_temp) <= 1e-4:
            return temp
        now = time.time()
        elapsed = now - self._last_set_temp_cmd_monotonic
        if elapsed < self.set_temp_min_interval_sec:
            pygame.time.wait(max(1, int((self.set_temp_min_interval_sec - elapsed) * 1000)))
        self.send_command(f"SET_TEMP,{temp}")
        self.target_temp = temp
        self._last_set_temp_cmd_monotonic = time.time()
        return temp

    def set_temperature_attenuation(self, attenuation):
        """초당 감쇠 온도 설정"""
        self.attenuation = attenuation

    def temp_updown(self, temp_updown):
        new_temp = self.target_temp + temp_updown
        self.set_target_temperature(new_temp)

    def start_control(self):
        """아두이노의 자동 온도 제어를 시작합니다."""
        self.send_command("START")

    def stop_control(self):
        """아두이노의 자동 온도 제어를 중지합니다."""
        self.send_command("STOP")
    
    def close(self):
        """시리얼 연결을 닫습니다."""
        self.ser.close()
        print("Serial connection closed.")

if __name__ == '__main__':
    a = 'abc.json'
    while (1):
        b = int(input("Enter number you want to test\n[0] Display [1] Reward [2] Sensor [3] Exit\n"))
        if (b == 0):
            instance = Display(a)
            while (1):
                c = input("Enter screen location. To end enter 'e'\n").split(" ")
                if 'e' in c:
                    break
                if c == ['']: # this part is just for test
                    instance.show()
                instance.show(c)
        elif b == 1:
            instance = Reward(a)
            while (1):
                c = input("Enter number you want to test\n[0] reward led [1] reward port [2] exit \n")
                if c == "0":
                    while (1):
                        on = input("If you want to turn on led enter '1', to turn off enter '0', to exit enter 'e'")
                        if on == "0":
                            instance.light(False)
                        elif on == "1":
                            instance.light(True)
                        elif on == 'e':
                            break
                        else:
                            print("wrong input, please try again\n")
                elif c == "1":
                    while (1):
                        duration = input("Enter reward duration you want to give. To end Enter 'e'")
                        if duration == 'e':
                            break
                        instance.give(float(duration))
                        print(str(duration) + "s given")
                elif c == "2":
                    break
                else:
                    print("Wrong input. Please try again\n")
        elif b == 2:
            instance = Sensor(a)
            while (1):
                c = input("If you want to get data, enter '1'. To exit enter '2'")
                if c == "1":
                    print(instance.get())
                elif c == '2':
                    break
                else:
                    print("wrong input please try again\n")
        elif b == 3:
            break
        else:
            print("Wrong input")


