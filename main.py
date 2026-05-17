import cv2 as cv
import numpy as np
import math
import time
from ctypes import cast, POINTER
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
import screen_brightness_control as sbc
import mediapipe as mp
import pyautogui
import os
import sys

def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)


cap = cv.VideoCapture(0)
cap.set(cv.CAP_PROP_FRAME_WIDTH, 1200)
cap.set(cv.CAP_PROP_FRAME_HEIGHT, 700)

if not cap.isOpened():
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, "Could not access the webcam (Camera index 0). Please ensure your camera is plugged in and not in use by another application.", "HandWave Error", 0x10)
    sys.exit(1)

mp_hands = mp.solutions.hands
mp_draw = mp.solutions.drawing_utils
hands = mp_hands.Hands(min_detection_confidence=0.7, min_tracking_confidence=0.7)

devices = AudioUtilities.GetSpeakers()
interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
volume = cast(interface, POINTER(IAudioEndpointVolume))
volRange = volume.GetVolumeRange()
minVolume, maxVolume = volRange[0], volRange[1]

minBrightness, maxBrightness = 0, 100
prev_brightness = None
prev_volume = None
prev_gesture = None
show_osd = True  # Flag to display OSD overlays
prev_middle_y = None  # For scroll detection
click_cooldown = 0  # Timestamp of last click
scroll_cooldown = 0  # Timestamp of last scroll action
mouse_sensitivity = 1.5  # Scaling factor for cursor movement
scroll_sensitivity = 2   # Lines per pixel for scrolling


def is_fist(landmarks):
    return all(landmarks[i].y > landmarks[i - 2].y for i in range(8, 21, 4))

screen_width, screen_height = pyautogui.size()

# Function to move the cursor based on index fingertip position
def move_cursor(x, y):
    # Map webcam coordinates to screen size
    screen_x = np.interp(x, [0, w], [0, screen_width]) * mouse_sensitivity
    screen_y = np.interp(y, [0, h], [0, screen_height]) * mouse_sensitivity
    pyautogui.moveTo(int(screen_x), int(screen_y))

# Detect thumb up gesture to toggle OSD visibility
def is_thumb_up(landmarks):
    # Thumb tip higher (smaller y) than thumb MCP (landmark 2) and higher than wrist (landmark 0)
    return landmarks[4].y < landmarks[2].y and landmarks[4].y < landmarks[0].y

# Function to detect "L" gesture (thumb and index finger extended)
def is_L_gesture(landmarks):
    return (landmarks[4].x < landmarks[3].x and
            landmarks[8].y < landmarks[6].y and
            landmarks[12].y > landmarks[10].y)

# Function to detect swipe gestures
def detect_swipe(prev_x, curr_x):
    if prev_x and curr_x:
        if curr_x - prev_x > 50:
            pyautogui.press('nexttrack')  # Next track
        elif prev_x - curr_x > 50:
            pyautogui.press('prevtrack')  # Previous track

prev_x = None

last_volume_change = 0
last_brightness_change = 0

def overlay_icon(frame, icon, x, y, size=(40, 40)):
    """Overlays a resized icon onto the frame at position (x, y), handling transparency."""
    if icon is None:
        return

    icon_resized = cv.resize(icon, size)  # Resize icon
    icon_h, icon_w = icon_resized.shape[:2]

    # Ensure x and y are within the frame boundaries
    if y + icon_h > frame.shape[0] or x + icon_w > frame.shape[1]:
        return  # Skip if out of bounds

    if icon_resized.shape[2] == 4:  # If the icon has an alpha channel
        b, g, r, a = cv.split(icon_resized)  # Split channels
        mask = a / 255.0  # Normalize alpha to 0-1

        # Extract the region of interest (ROI) from the frame
        roi = frame[y:y + icon_h, x:x + icon_w]

        # Blend the icon with the frame using the alpha mask
        for c in range(3):  # Apply to B, G, R channels
            roi[:, :, c] = (1 - mask) * roi[:, :, c] + mask * icon_resized[:, :, c]

        frame[y:y + icon_h, x:x + icon_w] = roi  # Put back the blended ROI
    else:
        frame[y:y + icon_h, x:x + icon_w] = icon_resized  # Directly overlay if no alpha

# Load icons globally with error handling
try:
    speaker_icon = cv.imread(resource_path("speaker.png"), cv.IMREAD_UNCHANGED)
    if speaker_icon is not None:
        speaker_icon = cv.resize(speaker_icon, (40, 40))
    else:
        print("Warning: speaker.png not found. Volume icon will not be displayed.")

    brightness_icon = cv.imread(resource_path("brightness.png"), cv.IMREAD_UNCHANGED)
    if brightness_icon is not None:
        brightness_icon = cv.resize(brightness_icon, (40, 40))
    else:
        print("Warning: brightness.png not found. Brightness icon will not be displayed.")
except Exception as e:
    print(f"Error loading icons: {e}")
    speaker_icon = None
    brightness_icon = None

def draw_osd(frame, level, osd_type):
    osd_x, osd_y = 100, 50 if osd_type == "Volume" else 120
    osd_width, osd_height = 300, 50
    bar_width = int((level / 100) * osd_width)

    # Draw background and progress bar
    cv.rectangle(frame, (osd_x, osd_y), (osd_x + osd_width, osd_y + osd_height), (50, 50, 50), -1)
    cv.rectangle(frame, (osd_x, osd_y), (osd_x + bar_width, osd_y + osd_height), (0, 255, 0), -1)

    # Overlay the appropriate resized icon
    if osd_type == "Volume":
        overlay_icon(frame, speaker_icon, 50, osd_y)
    else:
        overlay_icon(frame, brightness_icon, 50, osd_y)

    # Display percentage
    percentage_text = f"{int(level)}%"
    text_x = osd_x + osd_width + 20  # Position text next to the bar
    text_y = osd_y + 35  # Align vertically with bar
    cv.putText(frame, percentage_text, (text_x, text_y), cv.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)

def setVolume(dist, frame):
    global prev_volume, last_volume_change
    vol = np.interp(int(dist), [35, 215], [minVolume, maxVolume])
    volper = np.interp(dist, [50, 250], [0, 100])
    volume.SetMasterVolumeLevel(vol, None)
    
    last_volume_change = time.time()  # Update last volume change time

    if prev_volume is None or abs(prev_volume - volper) > 5:
        prev_volume = volper

def setBrightness(dist, frame):
    global prev_brightness, last_brightness_change
    brightness = np.interp(int(dist), [35, 230], [minBrightness, maxBrightness])
    briper = np.interp(dist, [50, 250], [0, 100])
    sbc.set_brightness(int(brightness))
    
    last_brightness_change = time.time()  # Update last brightness change time

    if prev_brightness is None or abs(prev_brightness - briper) > 5:
        prev_brightness = briper

screenshot_taken = False  # Track screenshot state

while True:
    success, frame = cap.read()
    if not success or frame is None:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, "Failed to read frame from webcam. The camera may have been disconnected.", "HandWave Error", 0x10)
        break
    frame = cv.flip(frame, 1)
    rgb_frame = cv.cvtColor(frame, cv.COLOR_BGR2RGB)
    results = hands.process(rgb_frame)

    if results.multi_hand_landmarks:
        for hand_landmarks in results.multi_hand_landmarks:
            mp_draw.draw_landmarks(frame, hand_landmarks, mp_hands.HAND_CONNECTIONS)
            landmarks = hand_landmarks.landmark
            h, w, c = frame.shape
            xr1, yr1 = int(landmarks[4].x * w), int(landmarks[4].y * h)
            xr2, yr2 = int(landmarks[8].x * w), int(landmarks[8].y * h)
            dist = math.hypot(xr2 - xr1, yr2 - yr1)

            if landmarks[17].x > landmarks[5].x:
                hand_side = "Right"
            else:
                hand_side = "Left"

            if hand_side == "Right":
                setVolume(dist, frame)
            elif hand_side == "Left":
                setBrightness(dist, frame)
            
            # Mute/Unmute with Fist Gesture
            if is_fist(landmarks):
                pyautogui.press('volumemute')
                cv.putText(frame, "Muted/Unmuted", (500, 90), cv.FONT_HERSHEY_COMPLEX, 1, (0, 255, 255), 3)
            
            # Screenshot with "L" Gesture
            if is_L_gesture(landmarks):
                pyautogui.screenshot("screenshot.png")
                cv.putText(frame, "Screenshot Taken", (550, 650), cv.FONT_HERSHEY_COMPLEX, 1, (255, 255, 0), 3)
                screenshot_taken = True  # Set flag to hide OSD

            # Swipe Gestures for Media Control
            curr_x = int(landmarks[9].x * w)  # Base of middle finger
            detect_swipe(prev_x, curr_x)
            prev_x = curr_x

    # Hide OSD when screenshot is taken
    if not screenshot_taken:
        if time.time() - last_volume_change < 2:
            draw_osd(frame, prev_volume, "Volume")

        if time.time() - last_brightness_change < 2:
            draw_osd(frame, prev_brightness, "Brightness")
    else:
        screenshot_taken = False  # Reset flag after skipping OSD once

    cv.imshow("Hand Gesture Control", frame)
    if cv.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv.destroyAllWindows()
