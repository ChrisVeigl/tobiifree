# Notes about setup and calibration  

with tobiifree in native or web use; see original repo: https://github.com/Aetherall/tobiifree

## Calibration file location and format

When using the gaze tracker natively under Linux via the `tobiifreed` daemon, a settings file is expected in the folder `~/.config/tobii.json`.

### Format of tobii.json

A typical `tobii.json` looks like:

```json
{
  "display_area": {
    "w_mm": 300,
    "h_mm": 190,
    "cx":  0,
    "cy":  -100,
    "z_mm": 0,
    "tilt": 0
  }
}
```

**Interpretation:**

- `w_mm`, `h_mm` – visible panel size dimension in mm. Could be measured using a ruler etc. directly from the screen.
- `cx`, `cy` – tracker position relative to the screen centre in mm (x right, y up). Anchor expressions work too:
  `cx`: `l` / `c` / `r`, `cy`: `t` / `c` / `b`, each with `+/- offset`; e.g. `"cy": "b - 10"` = 10 mm below the bottom edge.
- `z_mm` – plane depth (tracker position in z-direction in mm, negative: towards the user, positive: away from the user)
- `tilt` – degrees (0 = flush, negative: screen top tilted toward the user, positive: screen top tilted away from the user).

Please note that this interpretation of coordinates is differnt from the tobiifree web demo slider positions. Also, the content of the provided calibration file (`./calibrations/manual-2026-04-06.json`) represents manually tweaked settings for the web demo, and differs from the values in `~/.config/tobii.json`.

## Calibration procedure

Usually, the calibration flow is:
* measure the screen size and tracker location, and provide the data (in tobii.json for native/daemon use or via the Web SDK). The [tobiifree web demo](https://aetherall.github.io/tobiifree/) presents slides to adjust these parameters. Another strategy is a simple user dialog where screen size can be entered, such as in [gazeGrid demo](https://github.com/ChrisVeigl/gazeGrid). The screen size and tracker locations have to be provided only once, as they are stored pesistently in the .json file (for daemon) or in the browser cache (for web applications)  
* perform an on-device calibration (e.g. 5-point or 9-point calibration) and store the calibration blob (usually done automatically ba the calibration tool).
* a python calibration script for native use via tobiifreed is provided: `[tobiifree-calibrate](https://github.com/ChrisVeigl/tobiifree/tree/main/applications/tobiifree-calibrate). The calibration blob file is stored in the same folder. If a blob filename is given as a commandline argument, the stored calibration blob is applied.
* apply a recently stored calibration blob after the tracker is re-attached or a user profile shall be switched 

## Additional (SW-based) calibration layers
The calibration workbench in the fork by georgy-wyy (https://github.com/george-wyy/tobiifree) provides an additional layer for gaze data correction (client-side, using affine or poly transformation on top of the on-device calibration) - this is an interesting approach to improve sitations where the on-device calibration yields insufficient quality especially in certain screen regions 

## Mouse emulation with tobiifree-mouse

The native mouse emulation client [gaze_mouse](https://github.com/ChrisVeigl/tobiifree/tree/main/applications/python-mouse) sets the mouse cursor to the current gaze location via `uinput` and provides optional dwell clicking. It also features client-side calibration and a "head-assist mode" where gaze position can be corrected via small head movements. Additionally, offset correction points can be added on-demand (see README).  

The mouse activities can be controlled during operation by using a pipe (FIFO) which accepts commands like `toggle mouse emulation`, `start calibration`, `toggle head assist`. The provided `gaze_ctl` tool can be used to send these commands into the FIFO of the gaze_mouse application. The tool call can be bound to system-wide hotkeys easily (via the Linux Desktop keyboard settings).

## Firmware extraction

The Tobii firmware can be extracted from the .exe files provided with the Tobii driver (i suspected *Tobii.Service.exe* to be the correct file) but no .data section was found.
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

Still, i am unsure if this is correct and the flash tool would work ...

## Building / Running on RaspberryPi

### Install Nix and other dependecies on the RaspberryPi, mostly described in the Aetherall's readme:
* get Nix from nixos.org
```
curl --proto '=https' --tlsv1.2 -L https://nixos.org/nix/install | sh -s -- --daemon
```
* change the following line in flake.nix:
```
    # system = "x86_64-linux";
    system = "aarch64-linux";
```
* run the ./start_nix.sh script. (first time will install dependencies which will take a while)
* install the udev rules for granting USB access:
```
sudo cp assets/99-tobii.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```
* run ```npm install``` in the repository root folder to install vite for the web demo.

### Mouse emulation

because of the restrictions Wayland imposes to system-wide mouse cursor control, uinput was used, which needs its own udev rule:
```
sudo groupadd uinput
sudo usermod -aG uinput $USER
echo 'KERNEL=="uinput", GROUP="uinput", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

In case of access problems to uinput (for mouse emulation) make sure the module is loaded; if not, add it to modules-load.d using
```
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
```

### Keyboard / Hotkey binding

On raspian (using labwc), add keyboard hotkeys to  ~/.config/labwc/rc.xml, e.g.
```
<keyboard>
  <keybind key="A-m">
    <action name="Execute">
      <command>/home/pi/tobii/applications/python-mouse/gaze-ctl toggle_pause</command>
    </action>
  </keybind>
  <keybind key="A-c">
    <action name="Execute">
      <command>/home/pi/tobii/applications/python-mouse/gaze-ctl calibrate</command>
    </action>
  </keybind>
  <keybind key="A-h">
    <action name="Execute">
      <command>/home/pi/tobii/applications/python-mouse/gaze-ctl toggle_head_assist</command>
    </action>
  </keybind>
</keyboard>
```

## Troubleshooting 

### USB access problems in Chrome
USB access for the Web demo did not work in Chrome (Laptop running Ubuntu, althouth udev rules were correctly installed), unless I enabled the web browser access rights via snap:
(only relevant if the browser was installed via the snap package manager)

```
sudo snap connect chromium:raw-usb
```

### Camera access problems in Chrome under Android
The Web SDK can work under Android, given that your device supports, USB-OTG. You can attach the tracker via an USB-C/USB-A OTG adapter. Make sure to use Chrome, and to grand camera/mic access in the Android settings (else, the connection can not be established and you will see an unrelated error when trying to connect).
 
### tobiifree-overlay screen mapping issue
Wrong mappings for the gaze point overlay can have different reasons, such as GTK4 layer shell not being available:

```
just overlay
it appears your Wayland compositor does not support the Session Lock protocol
** (tobiifree-overlay:15852): WARNING **: 21:08:00.664: Failed to initialize layer surface, it appears your Wayland compositor doesn't support Layer Shell
```

Gtk4-layer-shell Wayland extension fail to initialize e.g. on Ubuntu with Gnome/Mutter.
Then, the overlay window silently falls back to a standard floating window with a much smaller size! 
Forcing the fallback standard window into Fullscreen (or into an undocked, floating window with fullscreen size because GTK refused to render fullscreen windows with transparent background) can be a good work-around for correct gazepoint position!
