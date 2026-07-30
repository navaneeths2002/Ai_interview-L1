import os
import sys
from dotenv import load_dotenv
import anthropic

# Load environment variables from .env file
load_dotenv()

api_key = os.getenv("ANTHROPIC_API_KEY")

if not api_key:
    print("[ERROR] ANTHROPIC_API_KEY not found in .env file.")
    sys.exit(1)

print("[INFO] ANTHROPIC_API_KEY loaded successfully.")

# Initialize Anthropic client
client = anthropic.Anthropic(api_key=api_key)

def check_credit_balance(client: anthropic.Anthropic):
    """
    Checks Anthropic account credit balance / billing status by sending a minimal test probe
    and analyzing the API status / billing error response.
    """
    print("\n--- Checking Anthropic Credit Balance & Billing Status ---")
    
    # Anthropic does not have a direct GET /v1/balance API endpoint for standard keys.
    # Credit status is checked by probing the API and evaluating billing responses.
    try:
        probe_response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}]
        )
        print("[BALANCE CHECK] Status: ACTIVE")
        print("[BALANCE CHECK] Credit Balance is sufficient to execute requests.")
        print(f"[BALANCE CHECK] Probe usage: {probe_response.usage.input_tokens} input, {probe_response.usage.output_tokens} output tokens.")
        return True, "Balance active"
    except anthropic.APIStatusError as e:
        err_msg = str(e.message)
        if "credit balance is too low" in err_msg.lower() or "insufficient_credits" in err_msg.lower() or e.status_code in (400, 402, 429):
            print("[BALANCE CHECK] Status: INSUFFICIENT CREDITS / BALANCE TOO LOW")
            print(f"[BALANCE CHECK] Details: {err_msg}")
            print("[ACTION REQUIRED] Please top up credits at Anthropic Console:")
            print("                 https://console.anthropic.com/settings/plans-and-billing")
            return False, err_msg
        else:
            print(f"[BALANCE CHECK] Status: API Error ({e.status_code}): {err_msg}")
            return False, err_msg
    except Exception as e:
        print(f"[BALANCE CHECK] Error probing balance: {e}")
        return False, str(e)

# Run credit balance check
balance_ok, balance_details = check_credit_balance(client)

# Models to test (defaulting to project's configured model, with standard fallbacks)
models_to_test = [
    "claude-haiku-4-5-20251001",
    "claude-3-5-haiku-20241022",
    "claude-3-haiku-20240307"
]

prompt = "Hello! Please reply with a brief friendly greeting and confirm which model you are."

print("\n--- Testing Anthropic Haiku Model Execution ---")

success = False
for model_name in models_to_test:
    try:
        print(f"\nAttempting request with model: '{model_name}'...")
        response = client.messages.create(
            model=model_name,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        
        reply = response.content[0].text.strip()
        usage = response.usage
        
        print(f"[SUCCESS] Response from '{model_name}':")
        print(f"  {reply}")
        print(f"\n[USAGE] Input tokens: {usage.input_tokens}, Output tokens: {usage.output_tokens}")
        success = True
        break

    except anthropic.APIStatusError as e:
        print(f"[WARN] APIStatusError ({e.status_code}) for model '{model_name}': {e.message}")
    except Exception as e:
        print(f"[ERROR] Error testing model '{model_name}': {e}")

if not success:
    if not balance_ok:
        print("\n[FAILED] Model test failed due to credit balance issue. Upgrade or add funds in Anthropic Console.")
    else:
        print("\n[FAILED] Failed to get a successful response from any attempted model.")
    sys.exit(1)

