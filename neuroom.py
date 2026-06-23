#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
Created on Tue Jul 23 17:18:12 2019

@author: chuljung kwak 
"""




import RPi.GPIO as GPIO
import time
import pickle
import pygame
import numpy as np
import cv2
import sys

pygame.init()

black = (0,0,0)

display_width = 800
display_height = 480

gameDisplay = pygame.display.set_mode((display_width,display_height), pygame.NOFRAME)
gameDisplay.fill(black)

pygame.mouse.set_visible(False)

GPIO.setmode(GPIO.BCM)

nose_poke_reward = 4
reward_motor = 17
reward_led = 18
nose_poke_left = 20
nose_poke_center = 26
nose_poke_right = 19
wrong_led = 27
ttl_in = 10
ttl_out = 22

GPIO.setwarnings(False)

GPIO.setup (nose_poke_reward, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
GPIO.setup (reward_motor, GPIO.OUT, initial = GPIO.LOW)
GPIO.setup (reward_led, GPIO.OUT, initial = GPIO.LOW)
GPIO.setup (nose_poke_left, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
GPIO.setup (nose_poke_center, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
GPIO.setup (nose_poke_right, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
GPIO.setup (wrong_led, GPIO.OUT, initial = GPIO.LOW)
GPIO.setup (ttl_in, GPIO.IN, pull_up_down = GPIO.PUD_DOWN)
GPIO.setup (ttl_out, GPIO.OUT, initial = GPIO.LOW)
GPIO.setup (9, GPIO.OUT, initial = GPIO.LOW)
GPIO.setup (10, GPIO.OUT, initial = GPIO.LOW)
GPIO.setup (11, GPIO.OUT, initial = GPIO.LOW)

poke_limit = 0.01

poke_on = 1
poke_off = 0

def read (file_name):
    
    pkl_file = open (file_name,'r')
    x = pickle.load(pkl_file)
    pkl_file.close()
    
    return x
    
def log(experiment_id, file_name,r):
    
    file_name = 'result/' + file_name
    
    output = open (file_name+'.pkl','wb')
    pickle.dump(r,output)
    output.close()
    
def trial_start(experiment_id, experiment, t_start, trial):
            
    r = {}
    r['experiment id'] = experiment_id
    r['experiment'] = experiment
    r['out_poke'] = dict(zip(['left','center','right','reward'],[[],[],[],[]]))
    r['trial_start'] = time.time()
    
    print (str(round((time.time() - t_start),3)) + ': trial ' +str(trial +1)+ ' starts')
    
    return r

def wrong_trial_start(experiment_id, experiment, t_start, trial, results):
            
    r = {}
    r['experiment id'] = experiment_id
    r['experiment'] = experiment
    r['out_poke'] = dict(zip(['left','center','right','reward'],[[],[],[],[]]))
    r['trial_start'] = time.time()
    
    print ('the number of correction in current trials is ' + str(len(results[-1])))
    print (str(round((time.time() - t_start),3)) + ': trial ' +str(trial +1)+ ' starts again')
    
    return r

def trial_initiate(r, t_start):
    
    GPIO.output(reward_led, GPIO.HIGH)        
    
    poke = GPIO.input(nose_poke_reward)
    
    poke_list = []
    
    while True:
        
        while poke == poke_off:
            
            poke = GPIO.input(nose_poke_reward)
            
            for location in ['left','center','right']:
                
                out_poke (r, t_start, location = location)
            
        if poke == poke_on:
            
            poke_list.append(time.time())
        
        while poke == poke_on:
            
            poke = GPIO.input(nose_poke_reward)
            
        if poke == poke_off:
            
            poke_list.append(time.time())
        
            if  poke_list[-1] - poke_list[0] > poke_limit:
                
                GPIO.output(reward_led, GPIO.LOW)
                r['trial_initiate'] = time.time()
                
                print (str(round((time.time() - t_start),3)) + ': trial initiate')
                
                return r
            
            else:
                
                poke_list = []
                
def opto_trial_initiate(r, t_start, ttl_on):
    
    GPIO.output(reward_led, GPIO.HIGH)        
    
    poke = GPIO.input(nose_poke_reward)
    
    poke_list = []
    
    initiate_start = time.time()
    
    while True:
        
        while poke == poke_off:
            
            if time.time() - initiate_start > 30 and ttl_on == 1:
            
                r = ttl_event_off(r,t_start)
            
                ttl_on = 0
            
            poke = GPIO.input(nose_poke_reward)
            
            for location in ['left','center','right']:
                
                out_poke (r, t_start, location = location)
            
        if poke == poke_on:
            
            poke_list.append(time.time())
        
        while poke == poke_on:
            
            poke = GPIO.input(nose_poke_reward)
            
        if poke == poke_off:
            
            poke_list.append(time.time())
        
            if  poke_list[-1] - poke_list[0] > poke_limit:
                
                GPIO.output(reward_led, GPIO.LOW)
                r['trial_initiate'] = time.time()
                
                print (str(round((time.time() - t_start),3)) + ': trial initiate')
                
                return r
            
            else:
                
                poke_list = []
                
def opto_trial_initiate_gpio(r, t_start, ttl_on, gpio):
    
    GPIO.output(reward_led, GPIO.HIGH)        
    
    poke = GPIO.input(nose_poke_reward)
    
    poke_list = []
    
    initiate_start = time.time()
    
    while True:
        
        while poke == poke_off:
            
            if time.time() - initiate_start > 30 and ttl_on == 1:
            
                r = ttl_event_off_gpio(r,t_start,gpio)
            
                ttl_on = 0
            
            poke = GPIO.input(nose_poke_reward)
            
            for location in ['left','center','right']:
                
                out_poke (r, t_start, location = location)
            
        if poke == poke_on:
            
            poke_list.append(time.time())
        
        while poke == poke_on:
            
            poke = GPIO.input(nose_poke_reward)
            
        if poke == poke_off:
            
            poke_list.append(time.time())
        
            if  poke_list[-1] - poke_list[0] > poke_limit:
                
                GPIO.output(reward_led, GPIO.LOW)
                r['trial_initiate'] = time.time()
                
                print (str(round((time.time() - t_start),3)) + ': trial initiate')
                
                return r
            
            else:
                
                poke_list = []
                
def trial_initiate_ttl_event(r, t_start, time_limit):
    
    GPIO.output(reward_led, GPIO.HIGH)        
    
    poke = GPIO.input(nose_poke_reward)
    
    poke_list = []
    
    while True:
        
        while poke == poke_off:
            
            
            for location in ['left','center','right']:
                
                out_poke (r, t_start, location = location)
            
        if poke == poke_on:
            
            poke_list.append(time.time())
        
        while poke == poke_on:
            
            poke = GPIO.input(nose_poke_reward)
            
        if poke == poke_off:
            
            poke_list.append(time.time())
        
            if  poke_list[-1] - poke_list[0] > poke_limit:
                
                GPIO.output(reward_led, GPIO.LOW)
                r['trial_initiate'] = time.time()
                
                print (str(round((time.time() - t_start),3)) + ': trial initiate')
                
                return r
            
            else:
                
                poke_list = []
                

def ttl_trigger(r, t_start):
    
    poke = GPIO.input(ttl_in)
    
    poke_list = []
    
    while True:
        
        while poke == poke_off:
            
            poke = GPIO.input(ttl_in)
            
            for location in ['reward','left','center','right']:
                
                out_poke (r, t_start, location = location)
            
        if poke == poke_on:
            
            poke_list.append(time.time())
        
        while poke == poke_on:
            
            poke = GPIO.input(ttl_in)
            
        if poke == poke_off:
            
            poke_list.append(time.time())
        
            if  poke_list[-1] - poke_list[0] > poke_limit:
                
                r['ttl_trigger'] = time.time()
                
                print (str(round((time.time() - t_start),3)) + ': ttl trigger')
                
                return r
            
            else:
                
                poke_list = []
                
def center_initiate(r, t_start):
    
    visual = "/home/pi/Desktop/autoshaping/lamp.bmp"
    
    x_coordinations, y_coodination = 305, 200
    
    evidence = pygame.image.load(visual)
    gameDisplay.blit(evidence, (x_coordinations, y_coodination))
    pygame.display.update()  
    
    poke = GPIO.input(nose_poke_center)
    
    while True:
        
        while poke == poke_off:
            
            poke = GPIO.input(nose_poke_center)
            
            for location in ['reward','left','right']:
                
                out_poke (r, t_start, location = location)
            
        if poke == poke_on:
            
                GPIO.output(reward_led, GPIO.LOW)
                r['trial_initiate'] = time.time()
                
                print (str(round((time.time() - t_start),3)) + ': trial initiate')
                
                gameDisplay.fill(black)
                pygame.display.update()
                
                return r
            
def reward_event(r, t_start, rd):
    
    GPIO.output(reward_led, GPIO.HIGH)
    GPIO.output(reward_motor, GPIO.HIGH)
    time.sleep(rd)
    GPIO.output(reward_motor, GPIO.LOW)
    r['reward_given'] = time.time()
    r['reward_duration'] = rd
    print (str(round((time.time() - t_start),3)) + ': reward given')
    
    time.sleep(0.1)
    #this code is written to prevent unwanted electrical signal which leads to false postive 'reward event'
    
    return r

def wrong_event(r, t_start, w_duration):
                
    gameDisplay.fill(black)
    pygame.display.update()
    
    wrong_time = time.time()
    
    GPIO.output(wrong_led, GPIO.HIGH)
    
    while time.time() - wrong_time  < w_duration:
        
        for location in ['reward','left','center','right']:
        
            out_poke (r, t_start, location = location)
        
    GPIO.output(wrong_led, GPIO.LOW)
     
    return r
    
def out_poke (r, t_start, location = 'left'):
    
    poke_dict = dict(zip(['reward','left','center','right'],[4,20,26,19]))

    poke_no = poke_dict[location]
    
    poke = GPIO.input(poke_no)
    
    poke_list = []
    
    if poke == poke_on:
        
        poke_list.append(time.time())
    
    while poke == poke_on:
        
        poke = GPIO.input(poke_no)
        
    if poke == poke_off:
        
        poke_list.append(time.time())
    
        if  poke_list[-1] - poke_list[0] > poke_limit:
        
            r['out_poke'][location].append(time.time())
            print (str(round((time.time() - t_start),3)) + ': ' + location+ ' poke')
            
            return r
        
        else:
            
            pass     
        
        
def reward_retrieval (r, t_start):
    
    poke = GPIO.input(nose_poke_reward)
    GPIO.output(reward_led, GPIO.HIGH)        
    
    poke = GPIO.input(nose_poke_reward)
    
    poke_list = []
    
    while True:
        
        while poke == poke_off:
            
            poke = GPIO.input(nose_poke_reward)
            
            for location in ['left','center','right']:
                
                out_poke (r, t_start, location = location)
            
        if poke == poke_on:
            
            poke_list.append(time.time())
        
        while poke == poke_on:
            
            poke = GPIO.input(nose_poke_reward)
            
        if poke == poke_off:
            
            poke_list.append(time.time())
        
            if  poke_list[-1] - poke_list[0] > poke_limit:
                
                GPIO.output(reward_led, GPIO.LOW)
                r['reward_retrieval'] = time.time()
                
                print (str(round((time.time() - t_start),3)) + ': reward retrieve')
                
                return r
            
            else:
                
                poke_list = []
                
def ttl_event_on (r, t_start):
    
    r['ttl'] = [time.time()]
    GPIO.output(ttl_out, GPIO.HIGH)
    print (str(round((time.time() - t_start),3)) + ': ttl on')
    
    return r

def ttl_event_on_gpio (r, t_start, gpio):
    
    r['ttl'] = [time.time()]
    r['ttl_gpio'] = gpio
    GPIO.output(gpio, GPIO.HIGH)
    input_no = {10:'1',9:'2',11:'3'}[gpio]
    print (str(round((time.time() - t_start),3)) + ': ttl '+input_no+' on')
    
    return r


def ttl_event_off (r, t_start):
    
    GPIO.output(ttl_out, GPIO.LOW)
    r['ttl'].append(time.time())
    print (str(round((time.time() - t_start),3)) + ': ttl off')
    
    
    return r

def ttl_event_off_gpio (r, t_start,gpio):
    
    GPIO.output(gpio, GPIO.LOW)
    r['ttl'].append(time.time())
    r['ttl_gpio'] = gpio
    print (str(round((time.time() - t_start),3)) + ': ttl off')
    
    
    return r

def continuous_ttl (on_time, off_time):
    
    while True:
    
        GPIO.output(ttl_out, GPIO.HIGH)
        time.sleep(on_time)
        GPIO.output(ttl_out, GPIO.LOW)
        time.sleep(off_time)
        
    
    

def inter_trial_interval (experiment_id, r, t_start, ITI):
    
    print (str(round((time.time() - t_start),3)) + ': ITI for ' + str(ITI) + ' sec')
    r['ITI'] = time.time()
    r['ITI_duration'] = ITI
    
    output = sys.stdout
    
    while True:
    
        for location in ['reward','left','center','right']:
        
            out_poke (r, t_start, location = location)
            
        output.write('\r' + 'ITI time: ' + str(time.time() - r['ITI'])[:5]+ '\r')
        output.flush()
        
        if time.time() - r['ITI'] > ITI:
            
            if 'ttl' in r:
                    
                r = ttl_event_off(r,t_start)
            
            print (str(round((time.time() - t_start),3)) + ': '+'trial done')
        
            print ('#'+str(r))
                   
            r['trial'] = str(time.time())[:10]
                   
            file_name = experiment_id + '/' + r['trial']
            #create file name 
            
            log(experiment_id, file_name, r)
            
            return r
        
def delay (r, t_start, ITI):
    
    print (str(round((time.time() - t_start),3)) + ': delay for ' + str(ITI) + ' sec')
    r['delay'] = time.time()
    r['delay_duration'] = ITI
    
    output = sys.stdout
    
    while True:
    
        for location in ['reward','left','center','right']:
        
            out_poke (r, t_start, location = location)
            
        output.write('\r' + 'delay time: ' + str(time.time() - r['delay'])[:5]+ '\r')
        output.flush()
        
        if time.time() - r['delay'] > ITI:
            
            print (str(round((time.time() - t_start),3)) + ': '+'delay done')
        
            return r
        
        
def random_index (choices, visual_indexes):
    
    if len(set(visual_indexes)) == 1:
        
        try:
        
            choices_ = choices[:]
            
            choices_.remove(visual_indexes[0])
            
            visual_indexes.append(np.random.choice(choices_))
            
            return visual_indexes
        
        except ValueError:
            
            visual_indexes.append(np.random.choice(choices))
        
            return visual_indexes
    
        
    else:
        
        visual_indexes.append(np.random.choice(choices))
        
        return visual_indexes
    
def correct_perc_cal (name, alist, t_start):
    
    
    if alist[0] != []:
        
        alist = [_[0] for _ in alist if _!=[]]
        
        correct_perc = str(alist.count('correct')*100.0/len(alist))[:5]
        
    else:
        
        correct_perc = 'no correct %'
        
    print (str(round((time.time() - t_start),3)) + ': correct % of ' + name + ' is ' + correct_perc)
    
def correct_perc_cal_no_print (name, alist, t_start):
    
    
    if alist[0] != []:
        
        alist = [_[0] for _ in alist if _!=[]]
        
        correct_perc = str(alist.count('correct')*100.0/len(alist))[:5]
        
    else:
        
        correct_perc = 'no correct %'
        
    return correct_perc
        
        
        
    
def average_cal (name, alist, t_start):
    
    
    if alist[0] != []:
        
        alist = [_[0] for _ in alist if _!=[]]
        
        correct_perc = str(np.mean(alist))[:5]
        
    else:
        
        correct_perc = 'no data'
        
    print (str(round((time.time() - t_start),3)) + ': average of ' + name + ' is ' + correct_perc)
    
def location_perc_cal (name, location,alist, t_start):
    
    if alist[0] != []:
        
        alist = [_[0] for _ in alist if _!=[]]
        
        location_perc = str(alist.count(location)*100.0/len(alist))[:5]
        
    else:
        
        location_perc = 'no correct %'
        
    print (str(round((time.time() - t_start),3)) + ': choice % of ' +location + ' by ' + name + ' is ' + location_perc)
    
def video_recording (experiment_id):
    
    try:
    
        cap = cv2.VideoCapture(0)
        fps = cap.get(cv2.CAP_PROP_FPS)
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        out = cv2.VideoWriter('result/'+ experiment_id + '.avi', fourcc, fps, (640, 360))
        
        output = sys.stdout
        
        r = []
        
        print ('video recording starts...')
        
        tails = ['    ','    ','.   ','.   ','..  ','..  ','... ','... ', '....', '....']
        
        print ('$$$'+str(time.time()))
        
        while True:
            
            ret, frame = cap.read()
            
            if ret == True:
                
                frame = cv2.resize(frame, (640,360), interpolation = cv2.INTER_AREA) 
                
                r.append(time.time())
                
                out.write(frame)
                
                tail = tails[int(len(r)%8)]
                
                output.write('\r'+'video record'+str(tail)+'\r')
                output.flush()
                
                if cv2.waitKey(25) & 0xFF == ord('q'):
                    
                    break
                
            else:
                
                break
            
    except KeyboardInterrupt:
        
        print (time.time())
        
        log(experiment_id, experiment_id  + '/time_stamp_' + experiment_id,r)
    
    cap.release()
    out.release()
            
    
def reference_recording (experiment_id):
    
    cap = cv2.VideoCapture(0)
    fps = cap.get(cv2.CAP_PROP_FPS)
    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    out = cv2.VideoWriter('result/'+ experiment_id + '_reference.avi', fourcc, fps, (640, 360))
    
    r = []
    
    print ('reference is recording...')
    
    while True:
        
        ret, frame = cap.read()
        
        if ret == True:
            
            frame = cv2.resize(frame, (640,360), interpolation = cv2.INTER_AREA) 
            
            out.write(frame)
            
            r.append(time.time())
            
            if cv2.waitKey(25) & 0xFF == ord('q'):
                
                break
        
            if len(r) == 100:
                
                break
            
        else:
            
            break
        
    print ('reference recording is done')
        
    cap.release()
    out.release()
    cv2.destroyAllWindows() 

def tone_generator (freq, duration, volume=1):
    
    fs = 44100       
    
    # generate samples, note conversion to float32 array
    samples = (np.sin(2*np.pi*np.arange(fs*duration)*freq/fs)).astype(np.float32)
    
    samples = np.array((samples, samples))
    
    tone = pygame.sndarray.make_sound(samples)
    
    tone.play(loops = 10)
    
    
    
    
        

    