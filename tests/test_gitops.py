import subprocess


def test_gitops():
    res = subprocess.run(["hjbh", "-l"])
    print(res.returncode)
    print(res.stderr)
    print(res.stdout)
