import os
import asyncio
from dotenv import load_dotenv
from src.bot import TradingBot
from py_clob_client_v2.client import ClobClient

def test_connection_simple():
    """测试基础网络连接（无需私钥）"""
    print("正在测试与 CLOB v2 的基础网络连接...")
    try:
        # 使用官方 SDK 测试连接
        client = ClobClient("https://clob.polymarket.com", key="", chain_id=137)
        ok_status = client.get_ok()
        server_time = client.get_server_time()
        print("✅ 基础网络连接成功！")
        print(f"服务器状态: {ok_status}")
        print(f"服务器时间: {server_time}")
        return True
    except Exception as e:
        print("❌ 基础网络连接失败！")
        print(f"错误信息: {e}")
        return False

def test_bot_initialization():
    """测试使用环境变量初始化 TradingBot"""
    print("\n正在测试 TradingBot 的初始化（需要 .env 配置）...")
    load_dotenv()
    
    private_key = os.getenv("PRIVATE_KEY")
    proxy_wallet = os.getenv("PROXY_WALLET")
    
    if not private_key or not proxy_wallet:
        print("⚠️ 未在 .env 中找到 PRIVATE_KEY 或 PROXY_WALLET，跳过 Bot 初始化测试。")
        print("💡 若要测试完整的 Bot 初始化，请将 .env.example 复制为 .env 并填入你的配置。")
        return
        
    try:
        bot = TradingBot(
            private_key=private_key,
            safe_address=proxy_wallet
        )
        if bot.is_initialized():
            print("✅ TradingBot 成功初始化！")
            print(f"Client 对象: {bot.client}")
        else:
            print("❌ TradingBot 初始化失败！")
    except Exception as e:
        print("❌ TradingBot 初始化时发生错误！")
        print(f"错误信息: {e}")

if __name__ == "__main__":
    print("="*50)
    print("Polymarket CLOB v2 连接测试")
    print("="*50)
    test_connection_simple()
    test_bot_initialization()
    print("="*50)
