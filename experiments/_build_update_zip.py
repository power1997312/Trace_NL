"""构建最小更新包: 覆盖文件 + 新增功能代码, 保持 Trace_NL/ 目录结构"""
import os, zipfile, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

root = r"e:\Trace_NL"
out_zip = r"e:\Trace_NL_update_20260819.zip"

# 1) 内容变化(覆盖) + 2) 新增功能代码
files = [
    # 变化文件
    "config.py",
    "requirements.txt",
    "core/excel_generator.py",
    "core/requirement_extractor.py",
    "core/text_matcher.py",
    "core/traceability_matrix.py",
    "generate_forward_matrix.py",
    "generate_forward_sd_matrix.py",
    "PDF 识别/src/pdfparser/headings.py",
    "PDF 识别/src/pdfparser/layout.py",
    "PDF 识别/src/pdfparser/tables.py",
    # 新增功能代码
    "app.py",
    "core/pdf_parser_adapter.py",
    "core/debug_dump.py",
    "core/__init__.py",
    # 新 pdfparser 模块(根目录, 含全部修复)
    "pdfparser/__init__.py",
    "pdfparser/cli.py",
    "pdfparser/document.py",
    "pdfparser/extract.py",
    "pdfparser/headings.py",
    "pdfparser/layout.py",
    "pdfparser/lists.py",
    "pdfparser/media.py",
    "pdfparser/models.py",
    "pdfparser/order.py",
    "pdfparser/output.py",
    "pdfparser/preprocess.py",
    "pdfparser/tables.py",
    # 回归测试
    "tests/test_cross_page_merge_regression.py",
    "tests/test_list_structure_regression.py",
]

# 校验所有文件存在
missing = [f for f in files if not os.path.isfile(os.path.join(root, f))]
if missing:
    print("缺失文件:")
    for f in missing:
        print(f"  {f}")
    sys.exit(1)

# 打包
with zipfile.ZipFile(out_zip, 'w', zipfile.ZIP_DEFLATED) as z:
    for rel in files:
        fp = os.path.join(root, rel)
        # 内网 zip 顶层是 Trace_NL/, 保持相同结构
        arcname = "Trace_NL/" + rel.replace('\\', '/')
        z.write(fp, arcname)

size = os.path.getsize(out_zip)
print(f"已生成: {out_zip}")
print(f"大小: {size/1024:.1f} KB")
print(f"文件数: {len(files)}")

# 列出内容
print("\n=== 包内容 ===")
with zipfile.ZipFile(out_zip) as z:
    for n in z.namelist():
        print(f"  {n} ({z.getinfo(n).file_size} B)")
