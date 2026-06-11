import asyncio
import os
from dotenv import load_dotenv
from maxapi_bootstrap import bootstrap_maxapi

bootstrap_maxapi()

from max_proto import MaxAuthFlow

load_dotenv()

async def init_auth():
    phone = os.getenv("MAX_DEVICE_ID") # Берем номер из этой переменной, как вы указали
    device_id = "max2tg-bridge" # ID устройства для Max
    
    if not phone or not phone.startswith("+"):
        print(f"Ошибка: Номер телефона в .env (MAX_DEVICE_ID={phone}) некорректен. Должен быть +7...")
        return

    flow = MaxAuthFlow.create(device_id=device_id)
    try:
        print(f"Запрашиваю код подтверждения для {phone}...")
        # request_code - синхронный метод в SDK
        result = await asyncio.to_thread(flow.request_code, phone)
        print(f"Код успешно отправлен!")
        print(f"Verify Token: {result.verify_token}")
        print("\nПОЖАЛУЙСТА, ПРИШЛИТЕ ПОЛУЧЕННЫЙ КОД ИЗ СМС.")
        
        # Сохраним токен во временный файл для второго шага
        with open("data/verify_token.txt", "w") as f:
            f.write(result.verify_token)
            
    except Exception as e:
        print(f"Произошла ошибка при запросе кода: {e}")

if __name__ == "__main__":
    if not os.path.exists("data"):
        os.makedirs("data")
    asyncio.run(init_auth())
