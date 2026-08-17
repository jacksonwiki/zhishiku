import asyncio
from langchain_mcp_adapters.client import MultiServerMCPClient

async def main():
    config = {
        "bing_search": {
            "transport": "streamable_http",
            "url": "https://mcp.api-inference.modelscope.net/04e7246390384b/mcp/"
        }
    }
    client = MultiServerMCPClient(config)
    tools = await client.get_tools()
    print(f"✅ 获取工具数量：{len(tools)}")
    print("-"*60)
    if not tools:
        print("❌ 没有工具，检查URL是否过期")
        return
    for idx,tool in enumerate(tools):
        print(f"【工具{idx+1}】")
        print(f"名称：{tool.name}")
        print(f"描述：{tool.description}")
        print("-"*60)

if __name__ == "__main__":
    asyncio.run(main())