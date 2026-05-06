#!/usr/bin/env python3
"""
Polymarket Token Allowances Setup Script

This script sets the necessary token approvals for trading on Polymarket.
You need to run this ONCE before you can trade via the API.

Requirements:
- Your wallet must have some POL (Polygon native token) for gas fees
- Your wallet should have USDC.e (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)

Usage:
    1. Set your private key in .env file as PRIVATE_KEY
    2. Run: python set_allowances.py
    
Note: This only needs to be done once per wallet address.
"""

import os
import sys
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

try:
    from web3 import Web3
    from web3.constants import MAX_INT
    from web3.middleware import ExtraDataToPOAMiddleware
except ImportError:
    print("Error: web3 package not installed.")
    print("Please run: pip install web3")
    sys.exit(1)


# Contract ABIs
ERC20_APPROVE_ABI = """[{"constant": false,"inputs": [{"name": "_spender","type": "address" },{ "name": "_value", "type": "uint256" }],"name": "approve","outputs": [{ "name": "", "type": "bool" }],"payable": false,"stateMutability": "nonpayable","type": "function"}]"""

ERC1155_SET_APPROVAL_ABI = """[{"inputs": [{ "internalType": "address", "name": "operator", "type": "address" },{ "internalType": "bool", "name": "approved", "type": "bool" }],"name": "setApprovalForAll","outputs": [],"stateMutability": "nonpayable","type": "function"}]"""

# Token addresses on Polygon
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e (bridged)
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"     # Conditional Tokens

# Exchange contract addresses
EXCHANGE_CONTRACTS = {
    "CTF Exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg Risk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Neg Risk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
    # Polymarket recently introduced Taker fees for short-term crypto markets. 
    # The Fee Modules act as proxies and require token approvals to collect fees.
    "Fee Module": "0xE3f18aCc55091e2c48d883fc8C8413319d4Ab7b0",
    "Neg Risk Fee Module": "0x78769D50Be1763ed1CA0D5E878D93f05aabff29e",
}

# Polygon RPC endpoints (try multiple in case one fails)
RPC_ENDPOINTS = [
    "https://polygon-rpc.com",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
]

CHAIN_ID = 137  # Polygon mainnet


def get_web3_connection():
    """Try to connect to Polygon using environment RPC or public endpoints."""
    # 1. Try env var first
    env_rpc = os.getenv("POLY_RPC_URL")
    if env_rpc:
        try:
            print(f"Connecting to custom RPC: {env_rpc.split('@')[-1] if '@' in env_rpc else env_rpc[:20]}...")
            if env_rpc.startswith("wss://") or env_rpc.startswith("ws://"):
                provider = Web3.WebsocketProvider(env_rpc, websocket_timeout=30)
            else:
                provider = Web3.HTTPProvider(env_rpc, request_kwargs={'timeout': 30})
            
            web3 = Web3(provider)
            if web3.is_connected():
                web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                print(f"✓ Connected to Polygon via custom RPC")
                return web3
        except Exception as e:
            print(f"  Failed to connect to custom RPC: {e}")

    # 2. Fallback to public endpoints
    print("Falling back to public RPC endpoints...")
    for rpc_url in RPC_ENDPOINTS:
        try:
            web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': 30}))
            if web3.is_connected():
                web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                print(f"✓ Connected to Polygon via {rpc_url}")
                return web3
        except Exception as e:
            print(f"  Failed to connect to {rpc_url}: {e}")
            continue
    
    raise ConnectionError("Could not connect to any Polygon RPC endpoint")



def send_and_wait(web3, raw_tx, priv_key, description):
    """Sign, send, and wait for a transaction."""
    try:
        signed_tx = web3.eth.account.sign_transaction(raw_tx, private_key=priv_key)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"  Transaction sent: {tx_hash.hex()}")
        
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status == 1:
            print(f"  ✓ {description} successful!")
            return receipt
        else:
            print(f"  ✗ {description} failed (reverted)")
            return None
            
    except Exception as e:
        print(f"  ✗ {description} failed: {e}")
        return None


def main():
    print("=" * 60)
    print("Polymarket Token Allowances Setup")
    print("=" * 60)
    print()
    
    # Get private key from environment
    priv_key = os.getenv("PRIVATE_KEY")
    if not priv_key:
        print("Error: PRIVATE_KEY not set in environment or .env file")
        print("Please set your MetaMask private key in the .env file:")
        print('  PRIVATE_KEY="0x..."')
        sys.exit(1)
        
    proxy_wallet = os.getenv("PROXY_WALLET")
    if not proxy_wallet:
        print("Error: PROXY_WALLET not set in environment or .env file")
        sys.exit(1)
    
    # Ensure key has 0x prefix
    if not priv_key.startswith("0x"):
        priv_key = "0x" + priv_key
        
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src import create_bot_from_env
    try:
        bot = create_bot_from_env()
    except Exception as e:
        print(f"Error initializing bot: {e}")
        sys.exit(1)
    
    if bot.config.use_gasless and bot.relayer_client:
        print("=" * 60)
        print("Gasless mode detected, but script will use standard EOA signature to execute via Proxy...")
        print("=" * 60)
        
    # Connect to Polygon
    print("Connecting to Polygon network...")
    try:
        web3 = get_web3_connection()
    except ConnectionError as e:
        print(f"Error: {e}")
        sys.exit(1)
        
    # We are dealing with Proxy Wallet
    print(f"\nEOA Wallet address (Signer): {web3.eth.account.from_key(priv_key).address}")
    print(f"Proxy Wallet address (Funder): {proxy_wallet}")
    
    # Try to approve using EOA sending execTransaction to the Safe
    print("\nUsing standard Ethereum tx to execute approvals directly on the Proxy Wallet (Safe)...")
    
    success_count = 0
    total_count = len(EXCHANGE_CONTRACTS) * 2
    
    # Setup contracts and ABIs
    proxy_addr = web3.to_checksum_address(proxy_wallet)
    eoa_addr = web3.eth.account.from_key(priv_key).address
    
    safe_abi = [{
        "inputs": [
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "value", "type": "uint256"},
            {"internalType": "bytes", "name": "data", "type": "bytes"},
            {"internalType": "uint8", "name": "operation", "type": "uint8"},
            {"internalType": "uint256", "name": "safeTxGas", "type": "uint256"},
            {"internalType": "uint256", "name": "baseGas", "type": "uint256"},
            {"internalType": "uint256", "name": "gasPrice", "type": "uint256"},
            {"internalType": "address", "name": "gasToken", "type": "address"},
            {"internalType": "address", "name": "refundReceiver", "type": "address"},
            {"internalType": "bytes", "name": "signatures", "type": "bytes"}
        ],
        "name": "execTransaction",
        "outputs": [{"internalType": "bool", "name": "success", "type": "bool"}],
        "stateMutability": "payable",
        "type": "function"
    }]
    safe_contract = web3.eth.contract(address=proxy_addr, abi=safe_abi)
    usdc_contract = web3.eth.contract(address=web3.to_checksum_address(USDC_E_ADDRESS), abi=ERC20_APPROVE_ABI)
    ctf_contract = web3.eth.contract(address=web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_SET_APPROVAL_ABI)
    
    # Construct Safe signature for EOA owner (v=1, r=owner_padded, s=0)
    r = Web3.to_bytes(hexstr=eoa_addr).rjust(32, b'\0')
    s = b'\0' * 32
    v = bytes([1])
    safe_signature = r + s + v
    
    for contract_name, contract_address in EXCHANGE_CONTRACTS.items():
        print(f"\n📄 {contract_name} ({contract_address})")
        print("-" * 40)
        
        target_spender = web3.to_checksum_address(contract_address)
        
        # 1. Approve USDC.e
        print(f"  Setting USDC.e allowance...")
        try:
            data_usdc = usdc_contract.encode_abi("approve", args=[target_spender, 2**256 - 1])
            nonce = web3.eth.get_transaction_count(eoa_addr, 'pending')
            
            raw_tx = safe_contract.functions.execTransaction(
                usdc_contract.address,
                0,
                Web3.to_bytes(hexstr=data_usdc),
                0, 0, 0, 0, "0x0000000000000000000000000000000000000000", "0x0000000000000000000000000000000000000000",
                safe_signature
            ).build_transaction({
                "chainId": CHAIN_ID,
                "from": eoa_addr,
                "nonce": nonce,
                "gasPrice": web3.eth.gas_price,
            })
            
            receipt = send_and_wait(web3, raw_tx, priv_key, "USDC.e approve via Proxy")
            if receipt:
                success_count += 1
        except Exception as e:
            print(f"  ✗ USDC.e approve failed: {e}")
            
        # 2. Set CTF approval
        print(f"  Setting Conditional Tokens approval...")
        try:
            data_ctf = ctf_contract.encode_abi("setApprovalForAll", args=[target_spender, True])
            nonce = web3.eth.get_transaction_count(eoa_addr, 'pending')
            
            raw_tx = safe_contract.functions.execTransaction(
                ctf_contract.address,
                0,
                Web3.to_bytes(hexstr=data_ctf),
                0, 0, 0, 0, "0x0000000000000000000000000000000000000000", "0x0000000000000000000000000000000000000000",
                safe_signature
            ).build_transaction({
                "chainId": CHAIN_ID,
                "from": eoa_addr,
                "nonce": nonce,
                "gasPrice": web3.eth.gas_price,
            })
            
            receipt = send_and_wait(web3, raw_tx, priv_key, "CTF setApprovalForAll via Proxy")
            if receipt:
                success_count += 1
        except Exception as e:
            print(f"  ✗ CTF setApprovalForAll failed: {e}")
            
    print("\n" + "=" * 60)
    print(f"Completed: {success_count}/{total_count} transactions successful")
    print("=" * 60)
    return
    
    # Check balances
    pol_balance = web3.eth.get_balance(pub_key)
    pol_balance_ether = web3.from_wei(pol_balance, 'ether')
    print(f"POL balance: {pol_balance_ether:.4f} POL")
    
    if pol_balance_ether < 0.01:
        print("\n⚠️  Warning: Low POL balance! You need POL for gas fees.")
        print("   Please add at least 0.1 POL to your wallet.")
        response = input("\nContinue anyway? (y/n): ")
        if response.lower() != 'y':
            sys.exit(1)
    
    # Check USDC.e balance
    usdc_abi = '[{"constant":true,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"payable":false,"stateMutability":"view","type":"function"}]'
    usdc_contract = web3.eth.contract(address=USDC_E_ADDRESS, abi=usdc_abi)
    usdc_balance = usdc_contract.functions.balanceOf(pub_key).call()
    usdc_balance_formatted = usdc_balance / 10**6  # USDC has 6 decimals
    print(f"USDC.e balance: ${usdc_balance_formatted:.2f}")
    
    print()
    print("This script will set approvals for:")
    print(f"  - USDC.e ({USDC_E_ADDRESS[:10]}...)")
    print(f"  - Conditional Tokens ({CTF_ADDRESS[:10]}...)")
    print()
    print("To the following exchange contracts:")
    for name, addr in EXCHANGE_CONTRACTS.items():
        print(f"  - {name}: {addr}")
    
    print()
    response = input("Proceed with setting allowances? (y/n): ")
    if response.lower() != 'y':
        print("Aborted.")
        sys.exit(0)
    
    print()
    
    # Create contract instances
    usdc = web3.eth.contract(address=USDC_E_ADDRESS, abi=ERC20_APPROVE_ABI)
    ctf = web3.eth.contract(address=CTF_ADDRESS, abi=ERC1155_SET_APPROVAL_ABI)
    
    # Get initial nonce
    nonce = web3.eth.get_transaction_count(pub_key, 'pending')
    print(f"Current nonce: {nonce}")
    
    success_count = 0
    total_count = len(EXCHANGE_CONTRACTS) * 2  # 2 approvals per contract
    
    for contract_name, contract_address in EXCHANGE_CONTRACTS.items():
        print(f"\n📄 {contract_name}")
        print("-" * 40)
        
        # 1. Approve USDC.e
        print(f"  Setting USDC.e allowance...")
        try:
            # Refresh nonce for safety
            nonce = web3.eth.get_transaction_count(pub_key, 'pending')
            
            raw_tx = usdc.functions.approve(
                contract_address, 
                int(MAX_INT, 0)
            ).build_transaction({
                "chainId": CHAIN_ID,
                "from": pub_key,
                "nonce": nonce,
                "gasPrice": web3.eth.gas_price,
            })
            
            receipt = send_and_wait(web3, raw_tx, priv_key, "USDC.e approve")
            if receipt:
                success_count += 1
            
        except Exception as e:
            print(f"  ✗ USDC.e approve failed: {e}")
        
        # 2. Set CTF approval
        print(f"  Setting Conditional Tokens approval...")
        try:
            # Refresh nonce for safety
            nonce = web3.eth.get_transaction_count(pub_key, 'pending')

            raw_tx = ctf.functions.setApprovalForAll(
                contract_address, 
                True
            ).build_transaction({
                "chainId": CHAIN_ID,
                "from": pub_key,
                "nonce": nonce,
                "gasPrice": web3.eth.gas_price,
            })
            
            receipt = send_and_wait(web3, raw_tx, priv_key, "CTF setApprovalForAll")
            if receipt:
                success_count += 1
            
        except Exception as e:
            print(f"  ✗ CTF setApprovalForAll failed: {e}")
    
    print()
    print("=" * 60)
    print(f"Completed: {success_count}/{total_count} transactions successful")
    print("=" * 60)
    
    if success_count == total_count:
        print("\n✅ All allowances set successfully!")
        print("   Your wallet is now ready for Polymarket API trading.")
        print("\n   Make sure you have USDC.e in your wallet to trade.")
        print(f"   USDC.e contract: {USDC_E_ADDRESS}")
    else:
        print("\n⚠️  Some transactions failed. Please check the errors above.")
        print("   You may need to retry or add more POL for gas fees.")


if __name__ == "__main__":
    main()

