#!/usr/bin/env python3
"""
Polymarket Cancel All Orders Script

This script cancels all open orders for the account configured in the .env file.
It uses the TradingBot class from the src module to handle the cancellation.

Usage:
    python scripts/cancel_all_orders.py
"""

import os
import sys
import asyncio
from dotenv import load_dotenv

# Add project root to path to allow importing from src
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.bot import TradingBot
from lib.terminal_utils import Colors

async def main():
    # Load environment variables
    load_dotenv()
    
    print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
    print(f"{Colors.CYAN}Polymarket Cancel All Orders{Colors.RESET}")
    print(f"{Colors.BOLD}{'='*60}{Colors.RESET}\n")
    
    # Get credentials from environment
    private_key = os.getenv("PRIVATE_KEY")
    proxy_wallet = os.getenv("PROXY_WALLET")

    if not private_key:
        print(f"{Colors.RED}Error: PRIVATE_KEY not found in .env{Colors.RESET}")
        return
    
    print(f"Private Key: {private_key[:10]}...{private_key[-4:]}")
    
    try:
        # Initialize bot
        print("Initializing bot...")
        bot = TradingBot(
            private_key=private_key,
            safe_address=proxy_wallet
        )
        
        if not bot.is_initialized():
            print(f"{Colors.RED}Error: Bot failed to initialize properly.{Colors.RESET}")
            return
            
        print(f"{Colors.GREEN}Bot initialized successfully.{Colors.RESET}")
        
        # Confirm with user
        print(f"\n{Colors.YELLOW}Are you sure you want to cancel ALL open orders? (y/n): {Colors.RESET}", end="")
        choice = input().lower()
        if choice != 'y':
            print("Operation cancelled by user.")
            return
            
        # Cancel all orders
        print("\nCancelling all orders...")
        result = await bot.cancel_all_orders()
        
        if result.success:
            print(f"{Colors.GREEN}Success: {result.message}{Colors.RESET}")
            if result.data:
                print(f"Response data: {result.data}")
        else:
            print(f"{Colors.RED}Failed: {result.message}{Colors.RESET}")
            
    except Exception as e:
        print(f"{Colors.RED}An unexpected error occurred: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
