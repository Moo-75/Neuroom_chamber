import serial
import time

class ArduinoController:
    def __init__(self, port='/dev/arduino', baudrate=115200, timeout=1):
        """
        아두이노와의 시리얼 통신을 초기화합니다.
        포트 이름은 환경에 맞게 '/dev/ttyACM0' 등으로 변경해야 할 수 있습니다.
        """
        self.ser = serial.Serial(port, baudrate, timeout=timeout)
        time.sleep(2)  # 아두이노 리셋 및 시리얼 준비 시간
        initial_message = self.ser.readline().decode('utf-8').strip()
        print(f"Arduino says: {initial_message}")
        if "Ready" not in initial_message:
            print("Warning: Arduino might not be ready.")

    def send_command(self, command, no_response = True):
        """명령을 보내고 아두이노의 첫 번째 응답 라인을 읽어 반환합니다."""
        full_command = f"{command}\n"
        self.ser.write(full_command.encode('utf-8'))
        if no_response:
            return
        # time.sleep(0.3) # 명령 처리 시간 보장
        while self.ser.in_waiting == 0:
            time.sleep(0.1)
            pass
        # 버퍼에 응답이 있는지 확인
        if self.ser.in_waiting > 0:
            response = self.ser.readline().decode('utf-8').strip()
            return response
        return None # 응답이 없는 경우

    def get_temperatures(self):
        """현재 온도 값 (센서1, 센서2)를 튜플로 반환합니다."""
        response = self.send_command("GET_TEMP", no_response = False)
        if response:
            try:
                # "24.50,25.10" 같은 문자열을 파싱
                temp1, temp2 = map(float, response.split(','))
                return temp1, temp2
            except (ValueError, IndexError):
                return None, None
        return None, None

    def set_target_temperature(self, temp):
        """목표 온도를 설정합니다."""
        response = self.send_command(f"SET_TEMP,{temp}")
        print(f"Set target command response: {response}")

    def start_control(self):
        """아두이노의 자동 온도 제어를 시작합니다."""
        response = self.send_command("START")
        print(f"Start command response: {response}")

    def stop_control(self):
        """아두이노의 자동 온도 제어를 중지합니다."""
        response = self.send_command("STOP")
        print(f"Stop command response: {response}")
    
    def close(self):
        """시리얼 연결을 닫습니다."""
        self.ser.close()
        print("Serial connection closed.")

# --- 실행 예제 ---
if __name__ == "__main__":
    
    target_temp = 10.0
    try:
        # 컨트롤러 객체 생성
        arduino = ArduinoController(port='/dev/arduino') # 실제 포트 이름으로 변경!

        # 1. 자동 제어 시작
        arduino.start_control()
        
        # 2. 목표 온도를 28.5도로 설정
        arduino.set_target_temperature(target_temp)
        time.sleep(1)

        # 3. 5초 동안 1초마다 현재 온도 측정 및 출력
        print("\n--- Starting temperature monitoring ---")
        i = 0
        while True:
            temps = arduino.get_temperatures()
            curr_temp = 0
            if temps[0] is not None:
                print(f"[{i+1}/5] Current Temps: Sensor1={temps[0]:.2f}°C, Sensor2={temps[1]:.2f}°C")
                curr_temp = (temps[0] + temps[1]) / 2
            else:
                print(f"[{i+1}/5] Failed to get temperature reading.")
            
            if curr_temp > target_temp - 0.1 and curr_temp < target_temp + 0.1:
                break
            time.sleep(1)
            i += 1
        print("--- Finished temperature monitoring ---\n")

        # 4. 자동 제어 중지
        arduino.stop_control()

    except serial.SerialException as e:
        print(f"Error: Could not open serial port. {e}")
        print("Is the Arduino connected? Is the port name ('/dev/arduino') correct?")
    
    finally:
        if 'arduino' in locals() and arduino.ser.is_open:
            arduino.close()
