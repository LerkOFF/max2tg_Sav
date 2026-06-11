import asyncio
import os
from dotenv import load_dotenv
from maxapi_bootstrap import bootstrap_maxapi

bootstrap_maxapi()

from max_proto import MaxAuthFlow

load_dotenv()

async def complete_auth(code: str):
    device_id = "max2tg-bridge"
    
    try:
        with open("data/verify_token.txt", "r") as f:
            verify_token = f.read().strip()
    except FileNotFoundError:
        print("Ошибка: Файл data/verify_token.txt не найден.")
        return

    flow = MaxAuthFlow.create(device_id=device_id)
    try:
        print(f"Проверяю код {code}...")
        # Используем метод check_code напрямую
        # Посмотрим, что он возвращает через низкоуровневый клиент, чтобы не падать сразу
        fresh_auth = flow.fresh_auth
        result = await asyncio.to_thread(fresh_auth.check_code, verify_token=verify_token, verify_code=code)
        
        if result.login_token:
            print("Найден токен логина. Создаю SDK...")
            sdk = flow.sdk_from_code_result(result)
            bundle_path = "data/auth_bundle.json"
            sdk.save_auth_bundle(bundle_path)
            print(f"Авторизация успешно завершена! Bundle сохранен в {bundle_path}")
            profile = await asyncio.to_thread(sdk.login)
            print(f"Вы вошли как: {profile.get('payload', {}).get('firstName')} {profile.get('payload', {}).get('lastName')}")
        
        elif result.register_token:
            print("Аккаунт новый. Требуется завершение регистрации (confirm_profile).")
            print(f"Registration Token: {result.register_token}")
            
            # Мы можем сразу вызвать confirm_profile с дефолтными данными или спросить их
            # Для простоты попробуем подтвердить профиль
            first_name = "Max2TG"
            last_name = "Bridge"
            print(f"Завершаю регистрацию с именем: {first_name} {last_name}...")
            
            confirm_result = await asyncio.to_thread(
                flow.confirm_profile, 
                registration_token=result.register_token, 
                first_name=first_name, 
                last_name=last_name
            )
            
            sdk = flow.sdk_from_confirm_result(confirm_result)
            bundle_path = "data/auth_bundle.json"
            sdk.save_auth_bundle(bundle_path)
            print(f"Регистрация и авторизация завершены! Bundle сохранен в {bundle_path}")
            
        elif result.password_challenge:
            print("Требуется 2FA пароль. (password_challenge)")
            # Это пока не реализовано в нашем бридже
            print(f"Challenge: {result.password_challenge}")
        else:
            print("Неизвестный результат проверки кода.")
            print(result.raw)
            
    except Exception as e:
        print(f"Произошла ошибка: {e}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Использование: python complete_max_auth.py 123456")
    else:
        asyncio.run(complete_auth(sys.argv[1]))
