import argparse
import base64
import io
import os
import sys
import traceback
from contextlib import closing
from pathlib import Path
from typing import List

from bk7231tools.analysis import flash, rbl, utils
from bk7231tools.crypto.code import BekenCodeCipher
from bk7231tools.serial import BK7231Serial


def __add_serial_args(parser: argparse.ArgumentParser):
    parser.add_argument("-d", "--device", required=True, help="Serial device path")
    parser.add_argument(
        "-b",
        "--baudrate",
        type=int,
        default=115200,
        help="Serial device baudrate (default: 115200)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Timeout for operations in seconds (default: 10.0)",
    )
    return parser


def __ensure_output_dir_exists(output_dir):
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)
    return output_dir


def __generate_payload_output_file_path(dumpfile: str, payload_name: str, output_directory: str, extra_tag: str) -> str:
    dumpfile_name = Path(dumpfile).stem
    return os.path.join(output_directory, f"{dumpfile_name}_{payload_name}_{extra_tag}.bin")


def __decrypt_code_partition(partition: flash.FlashPartition, payload: bytes):
    CODE_PARTITION_COEFFICIENTS = base64.b64decode("UQ+wk6PL6txZk6F+x63rAw==")
    coefficients = (CODE_PARTITION_COEFFICIENTS[i:i+4] for i in range(0, len(CODE_PARTITION_COEFFICIENTS), 4))
    coefficients = tuple(int.from_bytes(i, byteorder='big') for i in coefficients)

    cipher = BekenCodeCipher(coefficients)
    padded_payload = cipher.pad(payload)
    return cipher.decrypt(padded_payload, partition.mapped_address)


def __carve_and_write_rbl_containers(dumpfile: str, layout: flash.FlashLayout, output_directory: str, extract: bool = False, with_rbl: bool = False) -> List[rbl.Container]:
    containers = []
    with open(dumpfile, "rb") as fs:
        indices = rbl.find_rbl_containers_indices(fs)
        if indices:
            print("RBL containers:")
            for idx in indices:
                print(f"\t{idx:#x}: ", end="")
                container = None
                try:
                    fs.seek(idx, os.SEEK_SET)
                    container = rbl.Container.from_bytestream(fs, layout)
                except ValueError as e:
                    print(f"FAILED TO PARSE - {e.args[0]}")
                if container is not None:
                    containers.append(container)
                    if container.payload is not None:
                        print(
                            f"{container.header.name} - [encoding_algorithm={container.header.algo.name}, size={len(container.payload):#x}]")
                        partition = layout.partitions[0]
                        for p in layout.partitions:
                            if p.name == container.header.name:
                                partition = p
                                break
                        if extract:
                            extra_tag = container.header.version
                            filepath = __generate_payload_output_file_path(
                                dumpfile=dumpfile, payload_name=container.header.name, output_directory=output_directory, extra_tag=extra_tag)
                            with open(filepath, "wb") as fsout:
                                container.write_to_bytestream(fsout, payload_only=(not with_rbl))

                            extra_tag = f"{container.header.version}_decrypted"
                            decryptedpath = __generate_payload_output_file_path(
                                dumpfile=dumpfile, payload_name=container.header.name, output_directory=output_directory, extra_tag=extra_tag)
                            with open(decryptedpath, "wb") as fsout:
                                fsout.write(__decrypt_code_partition(partition, container.payload))

                            print(f"\t\textracted to {output_directory}")
                    else:
                        print(f"{container.header.name} - INVALID PAYLOAD")
    return containers


def __scan_pattern_find_payload(dumpfile: str, partition_name: str, layout: flash.FlashLayout, output_directory: str, extract: bool = False):
    if not partition_name in {p.name for p in layout.partitions}:
        raise ValueError(f"Partition name {partition_name} is unknown in layout {layout.name}")

    final_payload_data = None
    partition = list(filter(lambda p: p.name == partition_name, layout.partitions))[0]
    with open(dumpfile, "rb") as fs:
        fs.seek(partition.start_address, os.SEEK_SET)
        data = fs.read(partition.size)
        i = partition.size
        while i > 0:
            datablock = data[i-16:i]
            # Scan for a block of 16 FF bytes, indicating padding at the end of a partition.
            # This is to ignore RBL headers and other metadata while scanning.
            if datablock == (b"\xFF" * 16):
                break
            i -= 16
        if i <= 0:
            raise ValueError(f"Could not find end of partition for {partition.name}")

        # Now do a pattern scan until we hit the first CRC-16 block
        # and the padding block right before it
        while i > 0:
            datablock = data[i-16:i]
            if datablock != (b"\xFF" * 16) and data[i-32:i-16] == (b"\xFF" * 16):
                # This is exactly after the last 0xFF padding block including its CRC-16 checksum
                i = (i - 16 + 2)
                break
            i -= 16
        payload = data[:i]

        # Extra check for dealing with weird dumps, this essentially
        # changes the pattern scan to purely a moving block read
        # and CRC validation from the start of the partition
        if not payload:
            fs.seek(partition.start_address, os.SEEK_SET)
            payload = fs.read(partition.size)

        block_io_stream = io.BytesIO(payload)
        final_payload = io.BytesIO()
        block = block_io_stream.read(32)
        first = True
        while block:
            crc_bytes = block_io_stream.read(2)
            if not utils.block_crc_check(block, crc_bytes):
                if first:
                    raise ValueError(f"First block level CRC-16 checks failed while analyzing partition {partition.name}")
                else:
                    # One of the CRC checks after the first one has failed, so either
                    # end of stream has been reached or the dump is mangled.
                    # In both cases, not much to do hence bail out assuming it's fine
                    break
            first = False
            final_payload.write(block)
            block = block_io_stream.read(32)

        final_payload_data = final_payload.getbuffer()

    if final_payload_data is not None:
        print(f"\t{partition.start_address:#x}: {partition.name} - [NO RBL, size={len(final_payload_data):#x}]")
        if extract:
            extra_tag = "pattern_scan"
            filepath = __generate_payload_output_file_path(dumpfile, payload_name=partition_name,
                                                           output_directory=output_directory, extra_tag=extra_tag)
            with open(filepath, "wb") as fs:
                fs.write(final_payload_data)

            extra_tag = "pattern_scan_decrypted"
            decryptedpath = __generate_payload_output_file_path(dumpfile, payload_name=partition_name,
                                                                output_directory=output_directory, extra_tag=extra_tag)
            with open(decryptedpath, "wb") as fs:
                fs.write(__decrypt_code_partition(partition, final_payload_data))
            print(f"\t\textracted to {output_directory}")

    return final_payload_data


def dissect_dump_file(args):
    dumpfile = args.file
    flash_layout = args.layout
    default_output_dir = os.getcwd()
    output_directory = args.output_dir or default_output_dir
    layout = flash.FLASH_LAYOUTS.get(flash_layout, None)

    if output_directory != default_output_dir and not args.extract:
        print("Output directory is different from default: assuming -e (extract) is desired")
        args.extract = True

    if args.extract:
        output_directory = __ensure_output_dir_exists(output_directory)

    containers = __carve_and_write_rbl_containers(dumpfile=dumpfile, layout=layout,
                                                  output_directory=output_directory, extract=args.extract, with_rbl=args.rbl)
    container_names = {container.header.name for container in containers if container.payload is not None}
    missing_rbl_containers = {part.name for part in layout.partitions} - container_names
    for missing in missing_rbl_containers:
        print(f"Missing {missing} RBL container. Using a scan pattern instead")
        __scan_pattern_find_payload(dumpfile, partition_name=missing, layout=layout,
                                    output_directory=output_directory, extract=args.extract)


def connect_device(device, baudrate, timeout):
    return BK7231Serial(device, baudrate, timeout)


def chip_info(device: BK7231Serial, args: List[str]):
    print(device.read_chip_info())


def read_flash(device: BK7231Serial, args: List[str]):
    with open(args.file, "wb") as fs:
        for data in device.flash_read(args.start_address, args.count * 4096, not args.no_verify_checksum):
            fs.write(data)


def parse_args():
    parser = argparse.ArgumentParser(
        prog="bk7231tools",
        description="Utilities to interact with BK7231 chips over serial and analyze their artifacts",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, help="subcommand to execute")

    parser_chip_info = subparsers.add_parser("chip_info", help="Show chip information")
    parser_chip_info = __add_serial_args(parser_chip_info)
    parser_chip_info.set_defaults(handler=chip_info)
    parser_chip_info.set_defaults(device_required=True)

    parser_read_flash = subparsers.add_parser("read_flash", help="Read data from flash")
    parser_read_flash = __add_serial_args(parser_read_flash)
    parser_read_flash.add_argument("file", help="File to store flash data")
    parser_read_flash.add_argument(
        "-s",
        "--start-address",
        dest="start_address",
        type=lambda x: int(x, 16),
        default=0x10000,
        help="Starting address to read from [hex] (default: 0x10000)",
    )
    parser_read_flash.add_argument(
        "-c",
        "--count",
        type=int,
        default=16,
        help="Number of 4K segments to read from flash (default: 16 segments = 64K)",
    )
    parser_read_flash.add_argument(
        "--no-verify-checksum",
        dest="no_verify_checksum",
        action="store_true",
        default=False,
        help="Must be used for BK7231N devices. Do not verify checksum of retrieved flash segments and fail if they do not match (default: False)",
    )
    parser_read_flash.set_defaults(handler=read_flash)
    parser_read_flash.set_defaults(device_required=True)

    parser_dissect_dump = subparsers.add_parser("dissect_dump", help="Dissect and extract RBL containers from flash dump files")
    parser_dissect_dump.add_argument("file", help="Flash dump file to dissect")
    parser_dissect_dump.add_argument("-l", "--layout", default="ota_1", help="Flash layout used to generate the dump file (default: ota_1)")
    parser_dissect_dump.add_argument("-O", "--output-dir", dest="output_dir", default="",
                                     help="Output directory for extracted RBL files (default: current working directory)")
    parser_dissect_dump.add_argument("-e", "--extract", action="store_true", default=False,
                                     help="Extract identified RBL containers instead of outputting information only (default: False)")
    parser_dissect_dump.add_argument("--rbl", action="store_true", default=False,
                                     help="Extract the RBL container instead of just its payload (default: False)")
    parser_dissect_dump.set_defaults(handler=dissect_dump_file)
    parser_dissect_dump.set_defaults(device_required=False)

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    try:
        if args.device_required:
            with closing(connect_device(args.device, args.baudrate, args.timeout)) as device:
                args.handler(device, args)
        else:
            args.handler(args)
    except TimeoutError:
        print(traceback.format_exc(), file=sys.stderr)
