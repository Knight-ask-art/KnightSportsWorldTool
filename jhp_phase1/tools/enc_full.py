import base64
import os
import subprocess
import sys
import time
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

UNIDBG = (r'D:\C-Drive-Archive\Downloads\聚合跑最新版（点进去下载）'
          r'\好想跑\apk_work\unidbg')
JAVA = r'D:\Codex\tools\jdk-21\jdk-21.0.11+10\bin\java.exe'

jars = [os.path.join(UNIDBG, 'libs', j) for j in os.listdir(os.path.join(UNIDBG, 'libs')) if j.endswith('.jar')]
cp = os.path.join(UNIDBG, 'out') + ';' + ';'.join(jars)

err_path = os.path.join(os.environ['TEMP'], 'of_err_full.txt')
errf = open(err_path, 'w', encoding='utf-8', errors='replace')
p = subprocess.Popen([JAVA, '-cp', cp, 'OracleFlow', 'oracle.apk'],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=errf, cwd=UNIDBG, text=True,
                     encoding='utf-8', errors='replace', bufsize=1)
while True:
    line = p.stdout.readline()
    if not line:
        break
    if line.strip() == 'READY':
        break

payload = base64.b64encode(b'{"test":1}').decode()
p.stdin.write('ENC:' + payload + '\n')
p.stdin.flush()
t1 = time.time()
while time.time() - t1 < 60:
    line = p.stdout.readline()
    if not line:
        break
    if line.startswith('OUT:'):
        print('OUT payload len:', len(line[4:].strip()))
        break
p.kill()
errf.close()

err = open(err_path, encoding='utf-8', errors='replace').read()
jni = [l for l in err.splitlines() if 'JNI' in l or 'Error' in l or 'error' in l or 'Exception' in l or 'fail' in l.lower()]
print('total stderr lines:', len(err.splitlines()), '| jni/err lines:', len(jni))
# print ALL JNI trace after READY-equivalent (the call part) - just print everything
for l in err.splitlines():
    if 'JDK_JAVA_OPTIONS' in l or 'swsport' in l:
        continue
    print(l[:190])