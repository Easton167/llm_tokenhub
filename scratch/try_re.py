import re

text = "name: 张三，age: 20，email: zhangsan@example.com"

m = re.search(r"name[:：]\s*(.+)", text)

print(m.group(0))
print(m.group(1))