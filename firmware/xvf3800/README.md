# reSpeaker XVF3800 firmware

The `.bin` images are not committed — they belong to Seeed and are versioned in
their own repo. Fetch them with:

```bash
B=https://raw.githubusercontent.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/master/xmos_firmwares
curl -sLO $B/usb/respeaker_xvf3800_usb_dfu_firmware_v2.1.0.bin
curl -sLO $B/i2s/respeaker_xvf3800_i2s_dfu_firmware_v1.0.7.bin
curl -sLO $B/i2s/application_xvf3800_i2s_slave_v1.0.8_16k.bin
curl -sLO $B/i2s/application_xvf3800_i2s_master_v1.0.8_48k.bin
```

## Flashing

Needs `dfu-util` (`brew install dfu-util`), and the **USB-C port nearest the
3.5 mm jack** — that one reaches the XMOS chip; the other does not.

```bash
dfu-util -l          # must list BOTH "reSpeaker DFU Upgrade" and "... Factory"
dfu-util -R -e -a 1 -D <firmware>.bin
```

The Factory entry is Safe Mode, and it is the only way back: **the I2S firmware
does not support USB DFU**. Confirm Factory is listed before flashing I2S, or
there is no recovery path.

`i2s_slave` pairs with the ESP32 as I2S master, which is how `xiao_main.cpp`
configures it. `i2s_master` is the opposite and will not work with that code.

Not needed for USB mode: the board enumerates as a mic-and-speaker audio device
out of the box, which is how `relay.py` currently drives it.
