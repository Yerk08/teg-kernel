import gpiod, time, threading, queue, keyboard, yaml, os
from datetime import timedelta
from gpiod.line import Edge, Direction

CONFIG_FILE = "config.yaml"

config_data = {
    "system": {
        "chip": 1,
        "line": 22,
        "interactive_mode": True,
        "use_sticky_keys": True,
        "sticky_settings": {
            "watchdog_timeout_ms": 150,
            "max_repeat_gap_ms": 1000
        }
    },
    "key_mapping": {}
}

config_lock = threading.Lock()
event_queue = queue.Queue()

# Состояния удержания клавиш
current_active_key = None
last_hex_code = None          
last_packet_time = 0          

def load_config():
    global config_data
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded = yaml.safe_load(f)
                if loaded and "system" in loaded and "key_mapping" in loaded:
                    config_data = loaded
                    print(f"Конфигурация загружена. Залипание клавиш: {config_data['system']['use_sticky_keys']}")
                    return
            print("Конфигурация повреждена. Перезапись дефолтными значениями.")
            save_config()
        except Exception as e:
            print(f"Ошибка чтения YAML: {e}.")
    else:
        print(f"Создан новый файл конфигурации {CONFIG_FILE}.")
        save_config()

def save_config():
    with config_lock:
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                yaml.safe_dump(config_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        except Exception as e:
            print(f"Ошибка записи в {CONFIG_FILE}: {e}")

class PeriodNECDecoder:
    def __init__(self):
        self.reset()

    def reset(self):
        self.is_recording = False
        self.intervals = []

    def handle_pulse(self, duration_us):
        # 1. Штатный интервал тишины между повторами (~90-125 мс)
        if 90000 < duration_us < 125000:
            self.reset()
            return "REPEAT"

        # 2. Первичный код повтора (NEC Repeat) сразу после старта (~10 - 13.4 мс)
        if 10000 < duration_us < 13400:
            self.reset()
            return "REPEAT"

        # 3. Стартовый маркер полноценного нового нажатия (~13.5 - 16.0 мс)
        if 13500 < duration_us < 16000:
            self.is_recording = True
            self.intervals = []
            return None

        # 4. Промежуточный импульс (дробление паузы шумом или переходные процессы)
        if duration_us >= 3000 and not self.is_recording:
            # Возвращаем длину импульса для логирования штатного восстановления
            return f"KEEP_WAITING:{int(duration_us)}"

        # 5. Сбор 32 бит данных
        if self.is_recording:
            if duration_us < 300:
                return None
                
            self.intervals.append(duration_us)

            if len(self.intervals) == 32:
                bits = [0 if x < 1750 else 1 for x in self.intervals]
                self.reset()
                
                val = sum((b << (31 - i)) for i, b in enumerate(bits))
                addr      = (val >> 24) & 0xFF
                addr_inv  = (val >> 16) & 0xFF
                cmd       = (val >> 8)  & 0xFF
                cmd_inv   =  val        & 0xFF

                # Проверка CRC
                if (addr ^ addr_inv == 0xFF) and (cmd ^ cmd_inv == 0xFF):
                    return f"0x{val:08X}"
                else:
                    # Ошибка CRC — только в этом случае пакет действительно битый
                    return f"BAD_PACKET_CRC:0x{val:08X}"
                    
        return None

def ir_hardware_listener(callback):
    decoder = PeriodNECDecoder()
    last_time = time.time_ns()
    chip_num = config_data["system"]["chip"]
    line_num = config_data["system"]["line"]
    
    with gpiod.request_lines(f'/dev/gpiochip{chip_num}', consumer="IR",
        config={line_num: gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.FALLING)}) as lines:
        
        while True:
            if lines.wait_edge_events(timedelta(seconds=2)):
                for _ in lines.read_edge_events():
                    now = time.time_ns()
                    hex_code = decoder.handle_pulse((now - last_time) / 1000)
                    last_time = now
                    if hex_code:
                        callback(hex_code)
            else:
                decoder.reset()

def release_key_watchdog():
    global current_active_key, last_packet_time, last_hex_code
    while True:
        time.sleep(0.05)
        if not config_data["system"]["use_sticky_keys"]:
            continue
            
        if current_active_key or last_hex_code:
            now_ms = time.time() * 1000
            time_since_last_signal = now_ms - last_packet_time
            sticky = config_data["system"]["sticky_settings"]
            
            if current_active_key and time_since_last_signal > sticky["watchdog_timeout_ms"]:
                print(f"Тайм-аут сигнала -> Клавиатура: Временно отжата [{current_active_key}]. Ожидание повтора...")
                keyboard.release(current_active_key)
                current_active_key = None
                
            if last_hex_code and time_since_last_signal > sticky["max_repeat_gap_ms"]:
                print(f"1 секунда тишины -> Код {last_hex_code} полностью забыт.")
                last_hex_code = None

def ir_action_callback(hex_code):
    global config_data, current_active_key, last_packet_time, last_hex_code
    
    now_ms = time.time() * 1000
    use_sticky = config_data["system"]["use_sticky_keys"]
    mapping = config_data["key_mapping"]
    interactive = config_data["system"]["interactive_mode"]

    # ШТАТНОЕ ВОССТАНОВЛЕНИЕ: В период ожидания повтора проскочил промежуточный импульс (шум)
    if hex_code.startswith("KEEP_WAITING:"):
        measured = hex_code.split(":")[1]
        if last_hex_code and use_sticky:
            # Не стираем память! Просто сообщаем, что фильтруем шум и продолжаем ждать REPEAT
            print(f"[⏳ ШТАТНОЕ ОЖИДАНИЕ] Импульс {measured} мкс (фильтрация помехи). Ожидаем следующий повтор...")
        return

    # 1. ОБРАБОТКА СИГНАЛА ПОВТОРА (REPEAT)
    if hex_code == "REPEAT":
        if not use_sticky or not last_hex_code:
            return  
            
        last_packet_time = now_ms
        if current_active_key:
            keyboard.press(current_active_key)
        elif last_hex_code in mapping:
            key = mapping[last_hex_code]
            if key != "UNKNOWN":
                print(f"[🔄 ШТАТНОЕ ВОССТАНОВЛЕНИЕ] Сигнал повтора успешно подхвачен! Снова зажата клавиша: [{key}]")
                current_active_key = key
                keyboard.press(key)
        return

    # 2. КРИТИЧЕСКАЕ ОШИБКА: ИСКАЖЕННЫЙ ЦЕЛЫЙ ПАКЕТ (ОШИБКА CRC)
    if hex_code.startswith("BAD_PACKET_CRC:"):
        print(f"[⚠️ КРИТИЧЕСКИЙ СБОЙ] Ошибка CRC 32-битного пакета: {hex_code.split(':')[1]}. Память удержания очищена.")
        # Только здесь затираем историю, так как прилетел некорректный код кнопки
        last_hex_code = None
        if current_active_key:
            keyboard.release(current_active_key)
            current_active_key = None
        return

    # 3. ОБРАБОТКА ПОЛНОЦЕННОГО НАЖАТИЯ (32 бита)
    if use_sticky:
        last_hex_code = hex_code  
        last_packet_time = now_ms

    if hex_code in mapping:
        key = mapping[hex_code]
        if key == "UNKNOWN":
            print(f"Кнопка {hex_code} имеет статус UNKNOWN.")
        else:
            if use_sticky:
                if current_active_key and current_active_key != key:
                    keyboard.release(current_active_key)
                print(f"ИК: {hex_code} -> [Зажата клавиша: {key}]")
                current_active_key = key
                keyboard.press(key)
            else:
                print(f"ИК: {hex_code} -> [Клик клавиши: {key}]")
                keyboard.send(key)
    else:
        if interactive:
            if current_active_key:
                keyboard.release(current_active_key)
                current_active_key = None
                
            print(f"\n[ОБУЧЕНИЕ] Обнаружен ИК-код: {hex_code}")
            print("--> Нажмите КЛАССИЧЕСКУЮ КЛАВИШУ на клавиатуре для привязки...")
            time.sleep(0.3) 
            captured_key = keyboard.read_key()
            
            print(f"[УСПЕХ] Код {hex_code} привязан к: [{captured_key}]")
            config_data["key_mapping"][hex_code] = captured_key
            save_config()
            print("Ожидание ИК-сигналов...\n")
        else:
            print(f"Новая кнопка занесена в key_mapping: {hex_code} -> UNKNOWN")
            config_data["key_mapping"][hex_code] = "UNKNOWN"
            save_config()

if __name__ == "__main__":
    print("=== ИК-Клавиатура 2.2 (Штатное восстановление сигналов повтора) ===")
    load_config()
    
    watchdog_thread = threading.Thread(target=release_key_watchdog, daemon=True)
    watchdog_thread.start()
    
    srv = threading.Thread(target=ir_hardware_listener, args=(event_queue.put,), daemon=True)
    srv.start()

    try:
        while True:
            code = event_queue.get()
            ir_action_callback(code)
    except KeyboardInterrupt:
        if current_active_key:
            keyboard.release(current_active_key)
        print("\nПрограмма остановлена.")

