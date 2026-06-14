from delta_client import DeltaClient
from config import API_KEY, API_SECRET, USE_TESTNET

c = DeltaClient(API_KEY, API_SECRET, USE_TESTNET)
print(f"Base URL : {c.base_url}")
print(f"Key (first 6 chars): {API_KEY[:6]}…")
print(f"Secret loaded: {bool(API_SECRET)}")
print(f"Wallets: {c.get_wallet_balances()}")
