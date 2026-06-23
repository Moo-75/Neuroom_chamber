#define THERMISTOR_PIN_1 A0
#define THERMISTOR_PIN_2 A1
#define TEMPERATURE_NOMINAL 25
#define THERMISTOR_NOMINAL 10000
#define NUM_SAMPLES 3
#define BCO_EFFICIENT 3950
#define SERIES_RESISTOR 10000

const int RPWM = 9;
const int LPWM = 10;
const int R_EN = 7;
const int L_EN = 8;
const int PWM_TOP = 3124;

bool readFloatArgument(String command, float &value);

// --- 변수 ---
float target_temperature = 25.0;
bool is_running = true;

// --- 논블로킹 타이머를 위한 변수 ---
unsigned long previousMillis = 0;
const long interval = 1000; // 제어 로직을 1000ms 간격으로 실행

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(20);

  // --- (핀 및 Timer1 설정은 기존과 동일) ---
  pinMode(RPWM, OUTPUT);
  pinMode(LPWM, OUTPUT);
  pinMode(R_EN, OUTPUT);
  pinMode(L_EN, OUTPUT);
  digitalWrite(R_EN, LOW);
  digitalWrite(L_EN, LOW);
  TCCR1A = _BV(WGM11) | _BV(COM1A1) | _BV(COM1B1);
  // Fast PWM mode 14, 1024x prescaling: 16 MHz / (1024 * (3124 + 1)) = 5 Hz.
  TCCR1B = _BV(WGM13) | _BV(WGM12) | _BV(CS12) | _BV(CS10);
  ICR1 = PWM_TOP;

  Serial.println("Arduino Ready. Command-based control.");
}


void loop() {
  // 1. 시리얼 명령은 매 루프마다 최대한 빨리 처리 (지연 없음)
  handleSerialCommands();
  // 2. 메인 로직(온도 자동 제어)은 정해진 간격(50ms)으로만 실행
  unsigned long currentMillis = millis();

  if (currentMillis - previousMillis >= interval) {
    previousMillis = currentMillis;

    if (is_running) {
      runTemperatureControl();
    } else {
      stopMotor();
    }
  }
}


// 시리얼 명령을 읽고 파싱하여 처리하는 함수
void handleSerialCommands() {
  if (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();

    if (command.startsWith("GET_TEMP")) {
      float temp1 = measure_temp(THERMISTOR_PIN_1);
      float temp2 = measure_temp(THERMISTOR_PIN_2);
      Serial.print(temp1, 4); // 소수점 4자리까지 정밀도 높여서 전송
      Serial.print(",");
      Serial.println(temp2, 4);
    }
    else if (command.startsWith("SET_TEMP")) {
      int commaIndex = command.indexOf(',');
      if (commaIndex != -1) {
        String tempValueStr = command.substring(commaIndex + 1);
        tempValueStr.trim();
        float parsed_temp;
        if (readFloatArgument(command, parsed_temp)) {
          target_temperature = parsed_temp;
        } else {
          Serial.println("ERR");
        }
        // 응답은 간단하게 처리 (선택사항)
        // Serial.print("OK: Target temperature set to ");
        // Serial.println(target_temperature);
      }
    }
    else if (command.equals("START")) {
      is_running = true;
      // Serial.println("OK: Motor control started.");
    }
    else if (command.equals("STOP")) {
      is_running = false;
      stopMotor();
      // Serial.println("OK: Motor control stopped.");
    }
    else if (command.equals("GET_TARGET")) {
      Serial.println(target_temperature, 4);
    }
  }
}

// --- 멀티존 비례 제어 (냉각/가열 별도 PWM, 4단계) ---
//
// 오차(error) = target - average_temp
// 냉각 필요: error < 0  → RPWM 구동
// 가열 필요: error > 0  → LPWM 구동
//
// Zone 경계 (절대 오차 기준):
//   Dead zone    : |error| <= 0.5°C  → 정지
//   Zone 1 (근접) : 0.5 < |error| <= 2.0°C
//   Zone 2 (중간) : 2.0 < |error| <= 3.0°C
//   Zone 3 (원거리): |error| > 3.0°C
//
// ICR1 = 3124 → 100% duty = 3124
// 냉각(COOLING)과 가열(HEATING) duty를 각 Zone별로 독립 설정

// ---- 냉각 duty 설정 ----
int cool_z1 = 2397;  // 76.7%
int cool_z2 = 2520;  // 80.0%
int cool_z3 = 2520;  // 80.0%

// ---- 가열 duty 설정 ----
int heat_z1 = 2141;  // 68.5%
int heat_z2 = 2312;  // 74.0%
int heat_z3 = 2520;  // 80.0%

bool readFloatArgument(String command, float &value) {
  int commaIndex = command.indexOf(',');
  if (commaIndex == -1) {
    return false;
  }

  String token = command.substring(commaIndex + 1);
  token.trim();
  if (token.length() == 0) {
    return false;
  }

  bool saw_digit = false;
  bool saw_dot = false;
  for (unsigned int i = 0; i < token.length(); i++) {
    char c = token.charAt(i);
    if (isDigit(c)) {
      saw_digit = true;
    } else if (c == '.' && !saw_dot) {
      saw_dot = true;
    } else if (c == '-' && i == 0) {
      continue;
    } else {
      return false;
    }
  }
  if (!saw_digit) {
    return false;
  }

  value = token.toFloat();
  return value > -50.0 && value < 100.0;
}

bool readSinglePercentCommand(String command, float &value) {
  int commaIndex = command.indexOf(',');
  if (commaIndex == -1) {
    return false;
  }

  String token = command.substring(commaIndex + 1);
  token.trim();
  if (token.length() == 0) {
    return false;
  }

  bool saw_digit = false;
  bool saw_dot = false;
  for (unsigned int i = 0; i < token.length(); i++) {
    char c = token.charAt(i);
    if (isDigit(c)) {
      saw_digit = true;
    } else if (c == '.' && !saw_dot) {
      saw_dot = true;
    } else {
      return false;
    }
  }
  if (!saw_digit) {
    return false;
  }

  value = token.toFloat();
  return value >= 0.0 && value <= 100.0;
}

void runTemperatureControl() {
  float temp1 = measure_temp(THERMISTOR_PIN_1);
  float temp2 = measure_temp(THERMISTOR_PIN_2);
  float average_temp = (temp1 + temp2) / 2.0;

  float error = target_temperature - average_temp;
  float abs_error = (error < 0) ? -error : error;

  if (abs_error <= 0.3) {
    stopMotor();
    return;
  }

  if (error < 0) {
    // 현재 온도가 목표보다 높음 → 냉각 (RPWM)
    int duty;
    if      (abs_error <= 0.5) duty = cool_z1;
    else if (abs_error <= 3.0) duty = cool_z2;
    else                       duty = cool_z3;
    controlMotor(duty, 0);
  } else {
    // 현재 온도가 목표보다 낮음 → 가열 (LPWM)
    int duty;
    if      (abs_error <= 0.5) duty = heat_z1;
    else if (abs_error <= 3.0) duty = heat_z2;
    else                       duty = heat_z3;
    controlMotor(0, duty);
  }
}

void controlMotor(int forward_duty, int backward_duty) {
  digitalWrite(R_EN, HIGH);
  digitalWrite(L_EN, HIGH);
  OCR1A = forward_duty;
  OCR1B = backward_duty;
}

void stopMotor() {
  digitalWrite(R_EN, LOW);
  digitalWrite(L_EN, LOW);
  OCR1A = 0;
  OCR1B = 0;
}

float measure_temp(int pin) {
  uint16_t samples[NUM_SAMPLES];
  float total = 0;

  for (int i = 0; i < NUM_SAMPLES; i++) {
    samples[i] = analogRead(pin);
    delayMicroseconds(100);
  }

  for (int i = 0; i < NUM_SAMPLES; i++) {
    total += samples[i];
  }

  float average_adc = total / NUM_SAMPLES;
  float resistance = SERIES_RESISTOR / (1023.0 / average_adc - 1.0);
  float steinhart;

  steinhart = log(resistance / THERMISTOR_NOMINAL) / BCO_EFFICIENT;
  steinhart += 1.0 / (TEMPERATURE_NOMINAL + 273.15);
  steinhart = 1.0 / steinhart - 273.15;

  return steinhart;
}
