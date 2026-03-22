# mscloud
- Python temp cloud uploader for bots (queue + polling) -   chat.monasa.net 公益项目   mschat功能之一云盘api版
# mscloud
chat.monasa.net登入后，在云盘页右上角三条杠里获取api key（免费）
一个面向机器人场景的临时云盘上传客户端。  
特点：单文件 `mscloud.py`、固定上传到 `https://chat.monasa.net`、支持队列并发、支持轮询回传链接。

## 安装要求

- Python 3.10+
- aiohttp

```bash
pip install aiohttp
```

## 快速开始

```python
import mscloud

client = mscloud.app(api_key="你的apikey", que_max=6)

client.load("a.mp4")
client.load("b.zip")

client.wait()
print(client.get("a.mp4"))
print(client.get("b.zip"))

client.close()
```

## 机器人主循环伪代码

```python
import mscloud

c = mscloud.app(api_key="你的apikey", que_max=6)
pending = {}  # {file_path: user_id}

while True:
    new_reqs = fetch_new_upload_requests()
    for req in new_reqs:
        c.load(req.file_path)
        pending[req.file_path] = req.user_id

    for path, user_id in list(pending.items()):
        file_url = c.get(path)
        if file_url != 0:
            send_file_link_to_user(user_id, file_url)
            pending.pop(path, None)

    sleep(0.2)
```

## API 说明

- `mscloud.app(api_key, que_max=3, timeout_seconds=180.0) -> MsCloudApp`
  - `que_max`：最大同时处理数（并发 worker）
- `client.load(file_path, filename=None, content_type=None, retry_count=None, retry_interval_seconds=None) -> None`
  - 仅入队，不阻塞
- `client.get(file_path) -> str | int`
  - 返回文件直链或 `0`
  - 读取后会移除该键，防止重复获取
- `client.wait(timeout=None) -> None`
  - 阻塞到当前队列任务处理完成
- `client.close() -> None`
  - 关闭客户端并释放资源

## 错误处理建议

- `0`：任务未完成或上传失败
- 建议业务层做超时重试与失败告警

## 免责声明

仅供合法合规场景使用，请遵守目标平台和当地法律法规。
