# Temperature_Chamber
Purpose of this project is to make a program that can run temperature association learning (TAL) and temperature reversal learning (TRL)

Core of this project is to make the neuroom chamber working with Peltier modules
1. Read and save floor temperature
2. PID controller of temperature
3. TAL and TRL

============================
cd ~/Desktop
git clone https://github.com/Moo-75/Neuroom_chamber.git
cd Neuroom_chamber
chmod +x setup_new_pi.sh
chmod +x link_arduino.sh
./setup_new_pi.sh
./link_arduino.sh
