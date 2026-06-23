task description

목표 task는 다음과 같음

처음에 trial 시작을 알리는 cue가 주어짐. (cue는 화면을 통한 cue가 될 수도 있고, reward.give(1)를 할 때 밸브 열리는 소리를 이용한 sound cue가 될 수도 있음)
그 후 일정 시간 동안 left 혹은 right를 choice할 수 있는 시간이 주어짐
left나 right를 choice하면 방향에 따라 온도가 상승하거나 하락함
left를 choice하면 온도가 상승하고 right를 choice하면 온도가 하락함
choice를 했을 때는 그 직후 sound cue와 함께 온도 변화 시간이 주어짐. 이 시간 동안은 choice를 할 수 없음
만약 시간 안에 어느 쪽도 선택하지 않았다면 no choice가 발생한 것으로 판단됨
no choice가 발생했을 때는 온도 변화없이 일정한 시간 동안 온도 유지가 이루어짐. 이 시간 동안은 choice를 할 수 없음
온도 변화 혹은 온도 유지 시간이 끝나면 다음 trial이 시작됨.
choice로 인한 온도변화는 결코 mouse에게 aversive한 경험을 주어서는 안됨. 그러므로 choice를 통해 온도가 올라가거나 내려가더라도
너무 높은 온도나 너무 낮은 온도로 온도가 변하지 않도록 온도 하한과 상한을 설정함.
예를 들어 optimal 온도 range가 25도에서 35도라면, choice로 인한 하한과 상한은 각각 23도와 37도로 설정함.

trial은 다음과 같은 구성으로 이루어짐
1. trial 시작을 알리는 cue
2. left나 right를 choice할 수 있는 시간
3. choice를 했을 때는 그 직후 sound cue와 함께 온도 변화 시간
4. no choice가 발생했을 때는 온도 변화없이 일정한 시간 동안 온도 유지
5. 온도 변화 혹은 온도 유지 시간이 끝나면 다음 trial이 시작됨

그러나 이렇게만 하면 몇 번의 choice만으로 금방 optimal 온도에 도달하여 더 이상 choice를 할 필요가 없게 될 수도 있음.
이때 attenuation이라는 개념이 적용되는데, attenuation은 mouse가 인지하지 못할 정도로 아주 느리게 온도가 변화하는 장치임.
mouse가 no choice를 하면 아주 천천히 attenuation이 적용되어서 온도가 올라가거나 내려가는데, attenuation은 다음 choice가 발생할 때까지 계속 적용됨.
no choice를 했을 때 attenuation이 적용되는 방향(온도 상승 or 하락)은 state로 결정되는데, state는 hot state와 cold state로 구분됨.
hot state는 온도가 올라가는 방향으로 attenuation이 적용되는 상태이고, cold state는 온도가 내려가는 방향으로 attenuation이 적용되는 상태임.
state는 optimal 온도 range에서 벗어나 있다가 optimal 온도 range에 도달할 때마다 state가 변함.
예를 들어 optimal 온도 range가 27도에서 33도라면, 현재온도 34도 에서 mouse가 cool choice를 해서 32도로 온도가 내려가면서 optimal 온도 range 안쪽으로 도달했다고 하면,
state가 cold state 혹은 hot state로 변함. (기본적으로는 랜덤하게 변하되, 뜨거운 온도에서 optimal로 돌아온 경우이므로 cold state로 변할 확률이 더 큼)
반대로 현재온도 26도 에서 mouse가 hot choice를 해서 28도로 온도가 올라가면서 optimal 온도 range 안쪽으로 도달했다고 하면,
state가 hot state 혹은 cold state로 변함. (기본적으로는 랜덤하게 변하되, 차가운 온도에서 optimal로 돌아온 경우이므로 hot state로 변할 확률이 더 큼)
optimal 온도 range 안에서 온도가 변화한다거나 optimal 온도 range를 벗어날 때는 state 변화 없음. state 변화는 오직 optimal 온도 range를 벗어나 있다가 optimal 온도 range에 도달할 때 발생함.
no choice가 발생하면 현재 state에 따라 attenuation이 적용되는 방향이 결정됨.

이로써 mouse는 trial을 통해 optimal 온도를 찾아가는 과정을 거치고, no choice를 통해 현재 온도에 머무르려고 한다면
attenuation이 적용되어서 mouse가 인지하지 못하는 사이, 자동적으로 optimal 온도를 벗어나게 되어 다시 choice를 해야 함.
attenuation으로 변화할 수 있는 온도에도 하한과 상한이 있음. 이는 choice로 인한 온도변화 하한과 상한과는 다르게 더 넓으며, 각각 10도와 50도로 설정됨.

위의 task를 학습하는 것이 목표인데, 한 번에 학습시킬 수는 없으므로 training이 필요함. training task는 어떻게 구성할지 고민이 필요한데, 관련 논문을 찾아보고 고안해볼 것.