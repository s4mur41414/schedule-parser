import os
import re
import logging
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Set, Tuple

import requests
from bs4 import BeautifulSoup
from ics import Calendar, Event

# Настройка логирования для вывода в консоль GitHub Actions
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# --- КОНСТАНТЫ ---
LOGIN_URL = "https://p.mrsu.ru/Account/Login?ReturnUrl=%2F"
TIMETABLE_URL_TEMPLATE = "https://p.mrsu.ru/Learning/TimeTable/TimeTable?view=week&tick={tick}"

# Опорная точка для расчета тиков .NET (понедельник, 31 августа 2026 г.)
REFERENCE_DATE = datetime(2026, 8, 31).date()
REFERENCE_TICK = 639237312000000000

# Количество тиков .NET в одной неделе (7 дней * 24 ч * 60 мин * 60 сек * 10 000 000 тиков/сек)
TICKS_PER_WEEK = 6048000000000

MONTHS_RU = {
    'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4,
    'мая': 5, 'июня': 6, 'июля': 7, 'августа': 8,
    'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12
}

def authenticate(session: requests.Session, username: str, password: str) -> bool:
    """
    Выполняет аутентификацию в ЭИОС МГУ.
    Извлекает скрытые поля формы (VIEWSTATE) и отправляет POST-запрос.
    """
    logger.info("Загрузка страницы авторизации...")
    response = session.get(LOGIN_URL)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, 'html.parser')
    login_data = {}
    
    for field_name in ['__VIEWSTATE', '__VIEWSTATEGENERATOR', '__EVENTVALIDATION']:
        input_tag = soup.find('input', {'name': field_name})
        if input_tag:
            login_data[field_name] = input_tag.get('value', '')

    login_data.update({
        'ctl00$MainContent$UserName': username,
        'ctl00$MainContent$Password': password,
        'ctl00$MainContent$RememberMe': 'on',
        'ctl00$MainContent$Btn_SignIn': 'Вход'
    })

    logger.info("Выполнение входа...")
    session.post(LOGIN_URL, data=login_data, allow_redirects=True)

    if '.AspNet.ApplicationCookie' not in session.cookies:
        logger.error("Ошибка аутентификации: файл cookie .AspNet.ApplicationCookie не найден.")
        return False
        
    logger.info("Аутентификация успешна.")
    return True

def calculate_week_ticks(weeks_to_parse: int) -> List[int]:
    """
    Рассчитывает список тиков .NET для запрашиваемых недель, 
    начиная с текущего понедельника.
    """
    today = datetime.now().date()
    current_monday = today - timedelta(days=today.weekday())

    # Защита от запроса данных ранее опорной даты
    if current_monday < REFERENCE_DATE:
        current_monday = REFERENCE_DATE

    days_diff = (current_monday - REFERENCE_DATE).days
    start_tick = REFERENCE_TICK + (days_diff * TICKS_PER_WEEK)

    logger.info(f"Расчет тиков: стартовая неделя {current_monday}, начальный тик {start_tick}")

    return [start_tick + (i * TICKS_PER_WEEK) for i in range(weeks_to_parse)]

def parse_week(html_content: str, current_year: int, current_month: int, group_name: str) -> List[Event]:
    """
    Парсит HTML-страницу расписания на одну неделю и возвращает список событий ICS.
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    panels = soup.find_all('div', class_='panel panel-default')
    events = []

    for panel in panels:
        heading = panel.find('h3', class_='panel-title')
        if not heading:
            continue

        # Извлечение даты из заголовка (например, "понедельник 31 августа")
        heading_text = heading.get_text(strip=True)
        date_match = re.search(r'(\d+)\s+([а-я]+)', heading_text)
        if not date_match:
            continue

        day_num = int(date_match.group(1))
        month_name = date_match.group(2)
        month_num = MONTHS_RU.get(month_name)
        if not month_num:
            continue

        # Корректное определение года для переходов между семестрами (декабрь -> январь)
        if current_month >= 8:
            year = current_year if month_num >= 8 else current_year + 1
        else:
            year = current_year if month_num < 8 else current_year - 1

        try:
            current_date = datetime(year, month_num, day_num).date()
        except ValueError:
            continue

        table = panel.find('table', class_='table-timetable')
        if not table:
            continue

        for row in table.find_all('tr'):
            cells = row.find_all('td')
            if len(cells) < 2:
                continue

            time_text = cells[0].get_text(strip=True)
            time_match = re.match(r'(\d{2}:\d{2})\s*[-–]\s*(\d{2}:\d{2})', time_text)
            if not time_match:
                continue

            start_time_str, end_time_str = time_match.group(1), time_match.group(2)

            info_cell = cells[1]
            if not info_cell.get_text(strip=True):
                continue

            # Извлечение названия предмета
            subject = None
            subject_link = info_cell.find('a')
            if subject_link:
                b_tag = subject_link.find('b')
                subject = b_tag.get_text(strip=True) if b_tag else subject_link.get_text(strip=True)
            
            if not subject:
                b_tag = info_cell.find('b')
                subject = b_tag.get_text(strip=True) if b_tag else None
                
            if not subject:
                continue

            # Извлечение аудитории с корректным разделителем пробелов
            room = ""
            room_link = info_cell.find('a', href=lambda h: h and '/Information/Room' in h)
            if room_link:
                room = room_link.get_text(separator=' ', strip=True)

            # Извлечение данных преподавателя
            teacher_full = ""
            teacher_short = ""
            teacher_link = info_cell.find('a', title=lambda t: t and t.startswith('Преподаватель:'))
            if teacher_link:
                teacher_full = teacher_link.get('title', '').replace('Преподаватель: ', '').strip()
                teacher_short = teacher_link.get_text(strip=True)

            try:
                start_dt = datetime.combine(current_date, datetime.strptime(start_time_str, "%H:%M").time())
                end_dt = datetime.combine(current_date, datetime.strptime(end_time_str, "%H:%M").time())
            except ValueError:
                continue

            # Явное указание часового пояса (Москва)
            tz_moscow = ZoneInfo("Europe/Moscow")
            start_msk = start_dt.replace(tzinfo=tz_moscow)
            end_msk = end_dt.replace(tzinfo=tz_moscow)

            event = Event()
            event.name = subject
            event.begin = start_msk
            event.end = end_msk
            
            # Формирование поля LOCATION: объединяем кабинет и краткое ФИО для видимости в Apple Calendar
            location_parts = []
            if room:
                location_parts.append(room)
            if teacher_short:
                location_parts.append(teacher_short)
            
            event.location = " | ".join(location_parts) if location_parts else "Не указано"
            
            # Формирование поля DESCRIPTION: полное ФИО и группа
            description_lines = []
            if teacher_full:
                description_lines.append(f"Преподаватель: {teacher_full}")
            description_lines.append(f"Группа: {group_name}")
            
            event.description = "\n".join(description_lines)

            events.append(event)

    return events

def main() -> None:
    username = os.getenv('MRS_USERNAME', '')
    password = os.getenv('MRS_PASSWORD', '')
    group_name = os.getenv('MRS_GROUP', '441-2')

    if not username or not password:
        logger.error("Критическая ошибка: переменные окружения MRS_USERNAME и MRS_PASSWORD не заданы.")
        exit(1)

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })

    if not authenticate(session, username, password):
        exit(1)

    weeks_to_parse = 4
    ticks = calculate_week_ticks(weeks_to_parse)
    
    all_events: List[Event] = []
    seen_events: Set[Tuple[str, str, str, str]] = set()
    now = datetime.now()

    for i, tick in enumerate(ticks, 1):
        url = TIMETABLE_URL_TEMPLATE.format(tick=tick)
        logger.info(f"Загрузка недели {i} из {weeks_to_parse}: {url}")
        
        response = session.get(url)
        if response.status_code != 200:
            logger.warning(f"Ошибка HTTP {response.status_code} при загрузке недели {i}. Пропуск.")
            continue

        events = parse_week(response.text, now.year, now.month, group_name)
        
        # Дедупликация событий на случай некорректных ответов сервера
        new_events_count = 0
        for ev in events:
            event_key = (ev.name, str(ev.begin), str(ev.end), ev.location)
            if event_key not in seen_events:
                seen_events.add(event_key)
                all_events.append(ev)
                new_events_count += 1
                
        logger.info(f"Неделя {i}: найдено {len(events)} записей, добавлено уникальных: {new_events_count}")

    # Формирование и сохранение ICS-файла
    calendar = Calendar()
    for event in all_events:
        calendar.events.add(event)

    output_file = 'schedule.ics'
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(str(calendar))

    logger.info(f"Готово. Успешно создано {len(all_events)} уникальных событий в файле {output_file}.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Необработанное исключение: {e}")
        traceback.print_exc()
        exit(1)