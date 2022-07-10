# bk7231tools
This is a collection of tools to interact with and analyze artifacts for BK7231 MCUs.

## Installation
Install the dependencies and set up a Python virtualenv using `pipenv install`. Subsequent invocations can be done in a shell context after executing `pipenv shell` or by using `pipenv run <command>`.

## ⚠️ WARNING⚠️
Please be aware that this software is provided without any guarantees from the authors. If you will still use it, then please be aware that:

1. You understand what the software is doing
2. You choose to use it at your own risk
3. The authors cannot be held accountable for any damages that arise.

## Usage
There are a couple of usage modes for this toolset. One invovles dissecting already extracted flash artifacts and therefore does not require interaction with the device. The other mode can be abstracted under "device interaction", which could involve chip identification, reading flash, etc.

### Flash reading
Ensure that the MCU is hooked up to a UART bridge such that:
- `UART_TXD1` on the MCU is hooked up to the `RXD` pin on the UART bridge
- `UART_RXD1` on the MCU is hooked up to the `TXD` pin on the UART bridge

Afterwards, hook up the `GND` and `3v3` line to the MCU off the bridge or some other power source. In case another power source is used, ensure the power source's `GND` line is tied to the UART bridge's `GND` line.
Once the devices are connected, invoke `python bk7231tools.py` with the correct virtual env enabled. For example, to read all internal flash contents (2 MB in size, that's 512 4K segments) off a BK7231T device hooked up to `/dev/ttyUSB0` and into the file `dump.bin`, use:

```sh
$ pipenv run python bk7231tools.py read_flash -d /dev/ttyUSB0 -s 0 -c 512 dump.bin
```

The toolset will then attempt to connect to the MCU and perform the requested operation. During the connection attempt process, it may be the case that the device is not reset (in case RTS signal is not hooked up as well). If that's the case, the connection will fail. In order to remedy this issue, manually reset the device by disconnecting its power (but not the UART bridge!) a few times after issuing the command.

#### Note for reading BK7231N devices' flash
Reading flash for BK7231N devices is slightly different, since the protocol seems to provide incorrect CRCs when requested. Currently, the toolset can read BK7231N flash information, but CRC validation is unfortunately not working. In order to work around this issue, the `read_flash` subcommand can be invoked with the `--no-verify-checksum` flag to skip CRC validation.

### Dissecting flash dumps
Once a flash dump has been acquired, it can be dissected into its constituents by invoking the `dissect_dump` subcommand. For example, to dissect and extract artifacts from the flash dump file produced by the command in [flash reading](#flash-reading):

```sh
$ pipenv run python bk7231tools.py dissect_dump -e -O dump_extract_dir dump.bin

RBL containers:
        0x10f9a: bootloader - [encoding_algorithm=NONE, size=0xdd40]
                extracted to dump_extract_dir
        0x129f0a: app - [encoding_algorithm=NONE, size=0xfd340]
                extracted to dump_extract_dir
```
The above command flags are `-e` to extract - otherwise only a listing is shown and `-O` to write the extracted files to the specified directory (`dump_extract_dir`).
Combined with `--rbl`, you can also extract fully reconstructed RBL files for later usage.

Extracted artifacts are dependent on the flash layout supplied, but usually there are two partitions `app` and `bootloader`. If an extracted partition is also a known encrypted code partition (e.g. `app`), its decrypted version is also extracted with the suffix `_decrypted.bin`.

## Contributors
- [Kuba Szczodrzyński - @kuba2k2](https://github.com/kuba2k2)
