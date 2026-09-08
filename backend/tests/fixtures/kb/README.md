# 知识库解析/分块测试夹具

含中文样例：`sample.{txt,md,html,csv,docx,xlsx,pdf}`，以及空/损坏/扫描件负例。

重新生成（需已 `pip install -e ".[dev]"`，PDF 需本机有中文字体）：

```bash
cd backend
python tests/fixtures/kb/generate_fixtures.py
```
