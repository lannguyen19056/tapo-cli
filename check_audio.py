import struct
import glob

files = glob.glob('/workspaces/tapo-cli/stream_output/*.ts')
if not files:
    print('No TS files found')
    exit()

data = open(files[0],'rb').read()
audio_pid = 0x45
pes_starts = 0

for i in range(0, min(len(data), 188*5000), 188):
    if data[i] != 0x47: continue
    pid = ((data[i+1] & 0x1f) << 8) | data[i+2]
    payload_unit_start_indicator = (data[i+1] & 0x40) != 0
    
    if pid == audio_pid and payload_unit_start_indicator:
        adaptation_field_control = (data[i+3] & 0x30) >> 4
        payload_offset = 4
        if adaptation_field_control in (2, 3):
            payload_offset += 1 + data[i+4]
            
        if payload_offset + 3 < 188:
            if data[i+payload_offset] == 0 and data[i+payload_offset+1] == 0 and data[i+payload_offset+2] == 1:
                stream_id = data[i+payload_offset+3]
                print(f'Found PES start for audio PID, stream_id: {hex(stream_id)}')
                pes_starts += 1
                if pes_starts > 5: break
