
import sys
import asyncio
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.bot import TradingBot
from src.config import Config, get_env

async def main():
    try:
        config = Config.from_env()
        private_key = get_env("PRIVATE_KEY")
        bot = TradingBot(config=config, private_key=private_key)
        print(f"Bot 初始化成功，代理钱包: {bot.config.safe_address}")
        
        if not bot.client:
            print("Client 未初始化")
            return
            
        print("正在检查余额和授权...")
        # py_clob_client_v2 BalanceAllowanceParams
        # signature_type=2 for Gnosis Safe
        # Need to import BalanceAllowanceParams and AssetType
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=2)
        
        res = await bot._run_in_thread(bot.client.get_balance_allowance, params)
        print("检查结果:", res)

    except Exception as e:
        print(f"初始化失败: {e}")

if __name__ == "__main__":
    asyncio.run(main())
