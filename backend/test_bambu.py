#!/usr/bin/env python3
"""
Bambu Printer Connection Test

Run this script to test connectivity to your Bambu printer.

Usage:
    python test_bambu.py <IP> <SERIAL> <ACCESS_CODE>
    
Example:
    python test_bambu.py 192.168.1.100 00M09A380700000 12345678

Where to find your printer info:
    - IP Address: Printer screen → Network, or check your router
    - Serial Number: Printer screen → Settings → Device Info
                     (or on the sticker on the printer)
    - Access Code: Printer screen → Settings → Network → Access Code
                   (you may need to enable LAN mode first)
"""

import sys
import time

def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    
    ip = sys.argv[1]
    serial = sys.argv[2]
    access_code = sys.argv[3]
    
    print(f"Testing connection to Bambu printer...")
    print(f"  IP: {ip}")
    print(f"  Serial: {serial}")
    print(f"  Access Code: {'*' * len(access_code)}")
    print()
    
    try:
        from bambu_adapter import BambuPrinter, PrinterState
        
        printer = BambuPrinter(ip=ip, serial=serial, access_code=access_code)
        
        print("Connecting...")
        if printer.connect():
            print("✓ Connected successfully!")
            print()
            
            # Wait for status update
            print("Fetching status...")
            time.sleep(3)
            
            status = printer.get_status()
            
            print()
            print("=" * 40)
            print("PRINTER STATUS")
            print("=" * 40)
            print(f"  State: {status.state.value}")
            print(f"  Bed temp: {status.bed_temp}°C (target: {status.bed_target}°C)")
            print(f"  Nozzle temp: {status.nozzle_temp}°C (target: {status.nozzle_target}°C)")
            
            if status.state == PrinterState.PRINTING:
                print()
                print("PRINT PROGRESS")
                print(f"  File: {status.current_file}")
                print(f"  Progress: {status.print_progress}%")
                print(f"  Layer: {status.layer_current}/{status.layer_total}")
                print(f"  Time remaining: {status.time_remaining_minutes} min")
            
            if status.ams_slots:
                print()
                print("AMS SLOTS")
                for slot in status.ams_slots:
                    if not slot.empty:
                        print(f"  Slot {slot.slot_number}: {slot.filament_type} - {slot.color} ({slot.remaining_percent}%)")
                    else:
                        print(f"  Slot {slot.slot_number}: Empty")
            
            print()
            print("=" * 40)
            
            printer.disconnect()
            print()
            print("✓ Test complete!")
            
        else:
            print("✗ Connection failed!")
            print()
            print("Troubleshooting:")
            print("  1. Is the printer on and connected to your network?")
            print("  2. Can you ping the IP address?")
            print("  3. Is the serial number correct? (check printer sticker)")
            print("  4. Is the access code correct? (check printer screen → Network)")
            print("  5. Is LAN mode enabled on the printer?")
            sys.exit(1)
            
    except ImportError:
        print("Error: paho-mqtt not installed")
        print("Run: pip install paho-mqtt")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
