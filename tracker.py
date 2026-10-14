import json
import ssl
import time
import logging
import threading
import sys
import os
from datetime import datetime
import tkinter as tk
from tkinter import scrolledtext, ttk, messagebox

import paho.mqtt.client as mqtt
import gspread

from settings_manager import load_settings, run_setup_wizard

# ------------------- Логирование -------------------
LOG_FILE = "tracker.log"

class TextHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.insert(tk.END, msg + '\n')
            self.text_widget.see(tk.END)
        self.text_widget.after(0, append)

# Глобальные переменные
root = None
text_log = None
stop_event = threading.Event()
monitoring_thread = None

settings = None
google_conf = None
key_file = spreadsheet_name = sheet_log_name = sheet_stock_name = sheet_printers_name = None
TEST_MODE = True
gc = None
spreadsheet = None
PRINTERS = []
SERIAL_TO_NAME = {}
clients = []
lock = threading.Lock()
STOCK_CACHE = {}
stock_lock = threading.Lock()

# ------------------- Основные функции -------------------
def load_config():
    global settings, google_conf, key_file, spreadsheet_name, sheet_log_name, sheet_stock_name, sheet_printers_name, TEST_MODE
    print("[DEBUG] load_config() вызвана")
    settings = load_settings()
    if settings is None:
        print("[DEBUG] Настроек нет, запускаем мастер")
        settings = run_setup_wizard()
        if settings is None:
            logging.error("Настройка не завершена. Выход.")
            sys.exit(1)
    google_conf = settings.get("google", {})
    key_file = google_conf.get("key_file")
    spreadsheet_name = google_conf.get("spreadsheet_name")
    sheet_log_name = google_conf.get("sheet_log", "Лог печати")
    sheet_stock_name = google_conf.get("sheet_stock", "Склад")
    sheet_printers_name = google_conf.get("sheet_printers", "Принтеры")
    TEST_MODE = settings.get("test_mode", True)
    print(f"[DEBUG] TEST_MODE загружен как: {TEST_MODE}")

def init_google():
    global gc, spreadsheet
    try:
        gc = gspread.service_account(filename=key_file)
        spreadsheet = gc.open(spreadsheet_name)
        logging.info("Подключение к Google Таблице успешно.")
    except Exception as e:
        logging.error(f"Не удалось подключиться к Google Таблице: {e}")
        messagebox.showerror("Ошибка", f"Не удалось подключиться к Google Таблице:\n{e}")
        sys.exit(1)

def ensure_sheets():
    sheets_needed = {
        sheet_log_name: ["Дата и время", "Номер заявки", "Имя файла", "Принтер", "Тип пластика", "Расход (г)"],
        sheet_stock_name: ["Тип пластика", "Всего закуплено (г)", "Потрачено (г)", "Остаток на складе (г)", "Критический лимит (г)"],
        sheet_printers_name: ["Название", "IP-адрес", "Access Code", "Серийный номер"]
    }
    for sheet_name, headers in sheets_needed.items():
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
            if not worksheet.get_all_values():
                worksheet.append_row(headers)
                logging.info(f"Лист '{sheet_name}' пуст, добавлены заголовки.")
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=100, cols=len(headers))
            worksheet.append_row(headers)
            logging.info(f"Лист '{sheet_name}' создан с заголовками.")

def load_printers_from_sheet():
    try:
        sheet_printers = spreadsheet.worksheet(sheet_printers_name)
        raw = sheet_printers.get_all_records()
        printers = []
        for rp in raw:
            if all(k in rp for k in ("Название", "IP-адрес", "Access Code", "Серийный номер")):
                printers.append({
                    "name": rp["Название"],
                    "ip": rp["IP-адрес"],
                    "access_code": str(rp["Access Code"]),
                    "serial": str(rp["Серийный номер"])
                })
        logging.info(f"Загружено {len(printers)} принтеров из таблицы.")
        return printers
    except Exception as e:
        logging.error(f"Ошибка загрузки принтеров: {e}")
        return []

def refresh_stock_cache():
    global STOCK_CACHE
    try:
        sheet_stock = spreadsheet.worksheet(sheet_stock_name)
        sklad_data = sheet_stock.get_all_records()
        new_cache = {}
        for row in sklad_data:
            material = row.get("Тип пластика")
            if material:
                try:
                    ostatok = float(row.get("Остаток на складе (г)", 0))
                    new_cache[material] = ostatok
                except (ValueError, TypeError):
                    new_cache[material] = 0.0
        with stock_lock:
            STOCK_CACHE = new_cache
        logging.info(f"Кэш остатков обновлён: {len(STOCK_CACHE)} позиций.")
    except Exception as e:
        logging.error(f"Ошибка обновления кэша остатков: {e}")

def get_stock(material_type):
    with stock_lock:
        return STOCK_CACHE.get(material_type, 0.0)

def check_stock_alerts():
    try:
        sheet_stock = spreadsheet.worksheet(sheet_stock_name)
        sklad_data = sheet_stock.get_all_records()
        for row in sklad_data:
            material = row.get("Тип пластика")
            try:
                ostatok = float(row.get("Остаток на складе (г)", 0))
                limit = float(row.get("Критический лимит (г)", 0))
                if ostatok <= limit:
                    logging.warning(f"КРИТИЧЕСКИЙ ОСТАТОК: {material} осталось {ostatok}г (лимит {limit}г)")
            except (ValueError, TypeError):
                continue
    except Exception as e:
        logging.error(f"Ошибка проверки остатков: {e}")

def on_message(client, userdata, msg):
    try:
        topic_parts = msg.topic.split('/')
        if len(topic_parts) < 2:
            return
        serial_num = topic_parts[1]
        printer_name = SERIAL_TO_NAME.get(serial_num, f"Неизвестный ({serial_num})")

        data = json.loads(msg.payload.decode('utf-8'))
        if "print" in data:
            print_info = data["print"]
            file_name = print_info.get("subtask_name", "")
            if file_name and print_info.get("gcode_state") == "RUNNING":
                order_id = file_name.split('_')[0]
                materials = print_info.get("ams_materials", [])
                current_time = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

                for mat in materials:
                    mat_type = mat.get("type", "Неизвестно")
                    weight = mat.get("weight_g", 0)
                    stock = get_stock(mat_type)
                    if stock < weight:
                        logging.warning(
                            f"[{printer_name}] НЕ ХВАТИТ ПЛАСТИКА! Для заявки {order_id} требуется {mat_type}: {weight}г, "
                            f"на складе осталось: {stock}г"
                        )
                    row_data = [current_time, order_id, file_name, printer_name, mat_type, weight]
                    try:
                        sheet_log = spreadsheet.worksheet(sheet_log_name)
                        sheet_log.append_row(row_data)
                        logging.info(f"[{printer_name}] Заявка {order_id} добавлена. {mat_type}: {weight}г")
                    except Exception as e:
                        logging.error(f"Ошибка записи в лог: {e}")
                check_stock_alerts()
    except Exception as e:
        logging.error(f"Ошибка обработки сообщения: {e}")

def connect_printers():
    global clients, TEST_MODE
    with lock:
        for c in clients:
            c.loop_stop()
            c.disconnect()
        clients.clear()

        if TEST_MODE:
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
            client.on_message = on_message
            try:
                client.connect("localhost", 1883, 60)
                client.subscribe("device/+/report")
                client.loop_start()
                clients.append(client)
                logging.info("[ТЕСТ] Подключен к локальному брокеру, слушаем все принтеры.")
            except Exception as e:
                logging.error(f"Не удалось подключиться к локальному брокеру: {e}")
        else:
            for p in PRINTERS:
                client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
                client.on_message = on_message
                client.username_pw_set("bblp", p["access_code"])
                client.tls_set(cert_reqs=ssl.CERT_NONE)
                client.tls_insecure_set(True)
                try:
                    client.connect_async(p["ip"], 8883, 60)
                    client.subscribe(f"device/{p['serial']}/report")
                    client.loop_start()
                    clients.append(client)
                    logging.info(f"[СЕТЬ] Подключен к {p['name']} ({p['ip']})")
                except Exception as e:
                    logging.error(f"Не удалось подключиться к {p['name']}: {e}")

def refresh_loop(interval=300):
    while not stop_event.is_set():
        time.sleep(interval)
        if stop_event.is_set():
            break
        logging.info("Периодическое обновление данных...")
        global PRINTERS, SERIAL_TO_NAME
        new_printers = load_printers_from_sheet()
        if new_printers:
            PRINTERS = new_printers
            SERIAL_TO_NAME = {p["serial"]: p["name"] for p in PRINTERS}
            connect_printers()
        else:
            logging.warning("Нет принтеров для обновления.")
        refresh_stock_cache()

def monitoring_main():
    global PRINTERS, SERIAL_TO_NAME
    print("[DEBUG] monitoring_main() запущена")
    load_config()
    print(f"[DEBUG] После load_config() TEST_MODE = {TEST_MODE}")
    logging.info(f"Режим работы: {'ТЕСТОВЫЙ (локальный брокер)' if TEST_MODE else 'БОЕВОЙ (реальные принтеры)'}")
    init_google()
    ensure_sheets()
    PRINTERS = load_printers_from_sheet()
    if not PRINTERS:
        logging.warning("Нет принтеров в таблице. Добавьте их и перезапустите программу.")
        messagebox.showwarning("Предупреждение", "Нет принтеров в таблице. Добавьте их и перезапустите.")
        return
    SERIAL_TO_NAME = {p["serial"]: p["name"] for p in PRINTERS}
    refresh_stock_cache()
    connect_printers()
    refresh_thread = threading.Thread(target=refresh_loop, args=(300,), daemon=True)
    refresh_thread.start()
    check_stock_alerts()
    logging.info("Мониторинг запущен. Ожидание сообщений от принтеров...")
    while not stop_event.is_set():
        time.sleep(1)
    for c in clients:
        c.loop_stop()
        c.disconnect()
    logging.info("Мониторинг остановлен.")

# ------------------- GUI -------------------
def setup_gui():
    global root, text_log, monitoring_thread

    root = tk.Tk()
    root.title("Мониторинг 3D-печати")
    root.geometry("900x600")
    root.resizable(True, True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ]
    )

    text_log = scrolledtext.ScrolledText(root, wrap=tk.WORD, font=("Courier", 10))
    text_log.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    text_handler = TextHandler(text_log)
    text_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(text_handler)

    frame_buttons = ttk.Frame(root)
    frame_buttons.pack(fill=tk.X, padx=5, pady=5)

    def on_settings():
        # Передаём parent=root, чтобы мастер открылся как Toplevel, а не как второй Tk().
        # Второй Tk() создавал независимый Tcl-интерпретатор, из-за чего IntVar (var_mode)
        # мог отдавать не то значение при сохранении, и test_mode не менялся на false.
        new_settings = run_setup_wizard(parent=root)
        if new_settings:
            logging.info("Настройки обновлены. Перезапуск мониторинга...")
            restart_monitoring()

    def restart_monitoring():
        global monitoring_thread
        print("[DEBUG] restart_monitoring() вызвана")
        stop_event.set()
        if monitoring_thread and monitoring_thread.is_alive():
            monitoring_thread.join(timeout=2)
        stop_event.clear()
        # Принудительно перечитываем настройки
        load_config()
        print(f"[DEBUG] После load_config() в restart_monitoring TEST_MODE = {TEST_MODE}")
        monitoring_thread = threading.Thread(target=monitoring_main, daemon=True)
        monitoring_thread.start()

    def on_clear():
        text_log.delete(1.0, tk.END)

    def on_exit():
        stop_event.set()
        root.quit()
        root.destroy()
        sys.exit(0)

    ttk.Button(frame_buttons, text="Настройки", command=on_settings).pack(side=tk.LEFT, padx=5)
    ttk.Button(frame_buttons, text="Очистить логи", command=on_clear).pack(side=tk.LEFT, padx=5)
    ttk.Button(frame_buttons, text="Выход", command=on_exit).pack(side=tk.RIGHT, padx=5)

    root.protocol("WM_DELETE_WINDOW", on_exit)

    monitoring_thread = threading.Thread(target=monitoring_main, daemon=True)
    monitoring_thread.start()

    root.mainloop()

if __name__ == "__main__":
    if "--setup" in sys.argv or "-s" in sys.argv:
        run_setup_wizard()
    setup_gui()