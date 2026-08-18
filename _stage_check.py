"""执行 git add 并检查待提交内容"""
import subprocess, os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.chdir(r"e:/Trace_NL")

print("=== git add -A ===")
r = subprocess.run(["git", "add", "-A"], capture_output=True, text=True, encoding="utf-8", errors="replace")
print("returncode:", r.returncode)
if r.stderr.strip():
    print("stderr:", r.stderr[:2000])

print("\n=== git status --short (统计) ===")
r = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, encoding="utf-8", errors="replace")
lines = [l for l in r.stdout.splitlines() if l.strip()]
print(f"共 {len(lines)} 项")
for l in lines:
    print(f"  {l}")

print("\n=== 已暂存文件大小统计 (>1MB 列出) ===")
r = subprocess.run(["git", "diff", "--cached", "--name-only"], capture_output=True, text=True, encoding="utf-8", errors="replace")
files = [f for f in r.stdout.splitlines() if f.strip()]
total = 0
big = []
for f in files:
    if os.path.isfile(f):
        sz = os.path.getsize(f)
        total += sz
        if sz > 1*1024*1024:
            big.append((f, sz))
print(f"暂存文件总数: {len(files)}, 总计 {total/1024/1024:.2f} MB")
print("\n>1MB 文件:")
for f, sz in sorted(big, key=lambda x: -x[1]):
    print(f"  {sz/1024/1024:.2f} MB  {f}")
