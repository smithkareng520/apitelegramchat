import requests

def get_openrouter_balance(api_key):
    url = "https://openrouter.ai/api/v1/auth/key"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # Raises an exception for 4xx/5xx errors
        data = response.json()
        print("API 响应结构:", data)  # Print the full response for debugging
        if "data" in data and "limit_remaining" in data["data"]:
            remaining = data["data"]["limit_remaining"] if data["data"]["limit_remaining"] is not None else 0
            return remaining
        return 0
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
        return 0

if __name__ == "__main__":
    YOUR_API_KEY = "REDACTED_OPENROUTER_API_KEY"
    remaining_credits = get_openrouter_balance(YOUR_API_KEY)
    print(f"剩余余额: ${remaining_credits:.3f} 美元")