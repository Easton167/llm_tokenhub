from fastapi import Depends, FastAPI

app = FastAPI()
counter = {"n": 0}

async def get_conn():
    counter["n"] += 1
    print(f"  [依赖] 开连接 #{counter['n']}")
    yield f"conn-{counter['n']}"
    print(f"  [依赖] 关连接 #{counter['n']}")

async def get_user(conn=Depends(get_conn)):
    print(f"  [依赖] get_user 用的是 {conn}")
    return "alice"

@app.get("/who")
async def who(user=Depends(get_user), conn=Depends(get_conn)):
    print(f"  [路由] user={user}, conn={conn}")
    return {"user": user, "conn": conn}