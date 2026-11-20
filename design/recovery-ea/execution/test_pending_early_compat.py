"""Exercise early pre-inventory fill ownership gate with broker-free stubs."""
from pathlib import Path
import re
import subprocess
import tempfile
BASE=Path(__file__).resolve().parent
text=(BASE/"RecoveryManualPendingEarly.mqh").read_text()
text=text[text.index("struct RecoveryManualPendingEarly"):text.rindex("#endif")]
text=text[:text.index("// Freeze terms")]+text[text.index("// Only an exact"):]
text=text.replace("string fields[];", "std::vector<string> fields;")
text=re.sub(r'\(string\)(frozen\.original\.ticket)',r'std::to_string(\1)',text)
fixture=(BASE/"test_pending_early.cpp").read_text().replace("// ACTUAL",text)
with tempfile.TemporaryDirectory() as d:
    f=Path(d)/"test.cpp"
    f.write_text(fixture)
    subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                    "-fno-sanitize-recover=all",str(f),"-o",d+"/test"],check=True)
    subprocess.run([d+"/test"],check=True)
