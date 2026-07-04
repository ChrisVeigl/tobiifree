

# Notes and first steps 

with tobiifree see original repo: https://github.com/Aetherall/tobiifree

## calibration file location and format

I supposed that the calibration file is expected in ~/.config/tobii.json
So first i copied the content of the provided calibration file (./calibrations/manual-2026-04-06.json) to ~/.config/tobii.json
but tobiifree-overlay seems to use a different format, e.g. defining the physical screen size in millimeters using w_mm / h_mm ?
I am now using this tobii.json for my laptop screen (sized 30 x 19 cm) but i needed to manually tweak values for cx, cy, z_mm and tilt, 
using some unintuitive values for cy an cx (as the tracker is centered on the lower edge of my laptop screen) - so i might be doing something wrong


```
{
  "display_area": {
    "w_mm": 300,
    "h_mm": 190,
    "cx": -30,
    "cy": -140,
    "z_mm": 50,
    "tilt": 0
  }
}
```

Question is if we could provide a calibration method that is accessible for people who cannot edit files or deal with complex settings / small UI elements!
Is the calibration workbench in this fork useful (and shall it be merged)? https://github.com/george-wyy/tobiifree


## tobiifree-overlay screen mapping issue
In my first try I got completely wrong mappings for the gaze point overlay.
I noted following warning messages when running "just overlay":

```
it appears your Wayland compositor does not support the Session Lock protocol
** (tobiifree-overlay:15852): WARNING **: 21:08:00.664: Failed to initialize layer surface, it appears your Wayland compositor doesn't support Layer Shell
```

It seems that the gtk4-layer-shell Wayland extension fails to initialize on my desktop (I am using Ubuntu with Gnome/Mutter).

I added log messages to main.zig and found that the gaze x/y coordinates looked good (normalized to 0..1 for the x/y gaze location when looking around at the screen),
but the mapped screen coordinates where completely wrong, e.g:

```
Info(overlay): gaze norm: x=0.200 y=0.358 | screen px: x=307 y=344 | screen=1536x960 (sample #709)
```

I noticed that the screen size was detected as 1536 x 960 - which is wrong, as I used a resolution of 1920 x 1200 (with a 1.25 scale factor).
The error was bigger than just the scale factor.. It turned out that because the GTK Layer Shell failed, the overlay window silently falls back to a standard floating window with a much smaller size! 

Following a suggestion by Gemini 3.1, I forced the fallback standard window into Fullscreen (and later into an undocked, floating window with fullscreen size because GTK refused to render fullscreen windows with transparent background).
Thus, I got a working gazepoint overlay which matches my actual gaze position quite well!

## Calibration procedure(s)

I am still unsure how to calibrate the system correctly (e.g. to a new user). 
I noticed the different calibration variants in the web demo, but I am not sure in which way they differ and how to use them correcty
(the goal would be to avoid manual tweaking of the tobii.json config file, and just trigger the calibration procedure for a (new) user, same as with the original Tobii driver.)

* how to use the sliders in order to adjust the setup correctly? 
* do the slider settings actually make a difference for the calibration parameters which are stored to the tobii tracker - or are these just relevant for the web GUI display?
* is it sufficient to run the (5- or 9-point) on-device-calibration? are these settings then automatically applied
* (how) can a tobii.json be generated / downloaded which could then be applied by tobiifreed ? 
* is there a way to run a calibration procedure without the Web GUI (e.g. via tobiifreed / socket)  

## Mouse emulation

I added a mouse emulation client (tobiifree-mouse) which sets the mouse cursor to the current gaze location and provides optional dwell clicking.


```
Usage: tobiifree-mouse [options]

Options:
  --click                 Enable dwell clicking (disabled by default)
  --click-radius <float>  Normalized radius for dwell bounding box (default: 0.05)
  --click-dwell-ms <int>  Time in ms gaze must remain in radius to click (default: 1000)
```


Because of the restrictions Wayland imposes to system-wide mouse cursor control, uinput was used, which needs its own udev rule:

```
sudo groupadd uinput
sudo usermod -aG uinput $USER
echo 'KERNEL=="uinput", GROUP="uinput", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

The mouse activities can be paused/unpaused using a system-wide hotkey (defined in the Linux Desktop keyboard settings) which sends the singal SIGUSR1 to the running task, using:

```
pkill -SIGUSR1 tobiifree-mouse 
```


## Firmware extraction

I tried to extract the firmware from the .exe files provided with the Tobii driver (i suspected *Tobii.Service.exe* to be the correct file) but no .data section ws found.
I tried different exe files from other installers, and this file worked:

```
Tobii.EyeTracker5.Offline.Installer_4.183.0.30025/Platform/platform_runtime_IS5LEYETRACKER5_service.exe
```

the following CAI containers could be extracted:

```
./extract_firmware /mnt/087EC1427EC128F0/Windows/System32/DriverStore/FileRepository/eyetracker5.inf_amd64_a62d02618eb4f265/platform_runtime_IS5LEYETRACKER5_service.exe fw_extracted/
loaded /mnt/087EC1427EC128F0/Windows/System32/DriverStore/FileRepository/eyetracker5.inf_amd64_a62d02618eb4f265/platform_runtime_IS5LEYETRACKER5_service.exe: 19998560 bytes
.data: raw_off=0x1184a00 raw_size=0x13a600 virt_addr=0x1187000
found 2 CAI container(s)
  [0] off=0x118c8d4 size=1209172 version=t2srv:02a1a6a977 -> fw_extracted//cai_0_t2srv_02a1a6a977.bin
  [1] off=0x12b3c28 size=45665 version=t2srv:02a1a6a977 -> fw_extracted//cai_1_t2srv_02a1a6a977.bin
```

Still, i am unsure if i want to try out the flash tool, as i don't want to brick the only ET-5 I have here ;)
does this look correct? - and: how can a ET-5 in runtime mode be put into bootloader mode for accepting new firmware?



## Other remarks
USB access for the Web demo did not work in Chrome (althouth udev rules were correctly installed), unless I enabled the web browser access rights via snap:
(only relevant if the browser was installed via the snap package manager)

```
sudo snap connect chromium:raw-usb
```
 
