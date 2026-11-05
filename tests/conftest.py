"""Shared pytest fixtures."""
import csv
import textwrap

import pytest


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    with (root / "sessions.csv").open("w", newline="") as f:
        csv.writer(f).writerows([['subject_id', 'scan_date'], ['S001', '7/25/2026'], ['S001', '7/25/2026'],
                                ['S001', '8/1/2026'], ['S002', '7/25/2026'], ['', ''], ['`', '7/25/2026']])
    return root


@pytest.fixture
def converter(tmp_path):
    script = tmp_path / 'fake_converter.py'
    script.write_text(textwrap.dedent('''
        import json,os,pathlib,struct,sys
        args=sys.argv
        out=pathlib.Path(args[args.index('-o')+1])
        h=bytearray(352)
        struct.pack_into('<i',h,0,348)
        struct.pack_into('<8h',h,40,3,2,2,1,1,1,1,1)
        struct.pack_into('<h',h,70,4)
        struct.pack_into('<h',h,72,16)
        struct.pack_into('<f',h,108,352)
        h[344:348]=b'n+1\\0'
        h[148:165]=b'FAKE_PRIVATE_NAME'
        h[228:243]=b'FAKE_PRIVATE_ID'
        stem='series_1.2.3_3_1'
        (out/(stem+'.nii')).write_bytes(h+struct.pack('<4h',1,2,3,4))
        (out/(stem+'.json')).write_text(json.dumps({'SeriesDescription':'AX Diffusion_ADC','PatientName':'FAKE_PRIVATE_NAME','PatientID':'FAKE_PRIVATE_ID','AccessionNumber':'FAKE_PRIVATE_ACCESSION','SeriesNumber':3,'EchoTime':0.03}))
        (out/(stem+'.bval')).write_text('0 1000')
        (out/(stem+'.bvec')).write_text('0 1\\n0 0\\n0 0\\n')
        print('PatientName=FAKE_PRIVATE_NAME')
        print('Conversion diagnostic: done')
        sys.exit(int(os.environ.get('FAKE_CONVERTER_EXIT','0')))
    '''))
    return script
