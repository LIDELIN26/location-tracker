"""
手机定位追踪App
功能：
- 设置手机号和默认车牌号
- 管理常用地址（矩形范围、简称、语音播报内容）
- 配置服务器地址、数据库表名、上传间隔
- 配置定位采集间隔、坐标保留小数位数
- 定时采集GPS位置，与上一条记录比较，有变化才存入本地SQLite
- 主界面显示当前日期、车牌号、手机号、驾驶员姓名
- 切换车牌号、开始/停止采集、查看记录、手动上传数据、设置
- 进入常用地址范围时语音播报提示
"""

import sqlite3
import json
import time
from datetime import datetime
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleboxlayout import RecycleBoxLayout
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.recyclegridlayout import RecycleGridLayout
from kivy.properties import BooleanProperty, ObjectProperty, StringProperty, ListProperty
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.clock import Clock
from kivy.utils import platform
from plyer import gps, tts
import requests


# ---------- 本地数据库操作 ----------
class LocalDB:
    def __init__(self, db_path="tracker.db"):
        self.conn = sqlite3.connect(db_path)
        self.init_tables()

    def init_tables(self):
        cur = self.conn.cursor()
        # 配置表
        cur.execute('''
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        # 常用地址表
        cur.execute('''
            CREATE TABLE IF NOT EXISTS addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alias TEXT,
                min_lat REAL,
                max_lat REAL,
                min_lon REAL,
                max_lon REAL,
                speech_text TEXT
            )
        ''')
        # 定位记录表
        cur.execute('''
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                plate TEXT,
                timestamp TEXT,
                latitude REAL,
                longitude REAL,
                address_alias TEXT,
                uploaded INTEGER DEFAULT 0
            )
        ''')
        self.conn.commit()

    def get_config(self, key, default=None):
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM config WHERE key=?", (key,))
        row = cur.fetchone()
        if row:
            return row[0]
        return default

    def set_config(self, key, value):
        cur = self.conn.cursor()
        cur.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
        self.conn.commit()

    def get_all_addresses(self):
        cur = self.conn.cursor()
        cur.execute("SELECT id, alias, min_lat, max_lat, min_lon, max_lon, speech_text FROM addresses")
        return [{"id": r[0], "alias": r[1], "min_lat": r[2], "max_lat": r[3],
                 "min_lon": r[4], "max_lon": r[5], "speech_text": r[6]} for r in cur.fetchall()]

    def add_address(self, alias, min_lat, max_lat, min_lon, max_lon, speech_text):
        cur = self.conn.cursor()
        cur.execute('''
            INSERT INTO addresses (alias, min_lat, max_lat, min_lon, max_lon, speech_text)
            VALUES (?,?,?,?,?,?)
        ''', (alias, min_lat, max_lat, min_lon, max_lon, speech_text))
        self.conn.commit()

    def delete_address(self, addr_id):
        cur = self.conn.cursor()
        cur.execute("DELETE FROM addresses WHERE id=?", (addr_id,))
        self.conn.commit()

    def insert_location(self, phone, plate, lat, lon, address_alias):
        cur = self.conn.cursor()
        ts = datetime.now().isoformat()
        cur.execute('''
            INSERT INTO locations (phone, plate, timestamp, latitude, longitude, address_alias, uploaded)
            VALUES (?,?,?,?,?,?,0)
        ''', (phone, plate, ts, lat, lon, address_alias))
        self.conn.commit()
        return cur.lastrowid

    def get_latest_location(self, phone, plate):
        cur = self.conn.cursor()
        cur.execute('''
            SELECT latitude, longitude FROM locations
            WHERE phone=? AND plate=?
            ORDER BY timestamp DESC LIMIT 1
        ''', (phone, plate))
        row = cur.fetchone()
        return row if row else None

    def get_all_locations(self, phone=None, plate=None):
        cur = self.conn.cursor()
        if phone and plate:
            cur.execute('''
                SELECT id, phone, plate, timestamp, latitude, longitude, address_alias, uploaded
                FROM locations WHERE phone=? AND plate=? ORDER BY timestamp DESC
            ''', (phone, plate))
        else:
            cur.execute('''
                SELECT id, phone, plate, timestamp, latitude, longitude, address_alias, uploaded
                FROM locations ORDER BY timestamp DESC
            ''')
        return cur.fetchall()

    def mark_uploaded(self, ids):
        cur = self.conn.cursor()
        for lid in ids:
            cur.execute("UPDATE locations SET uploaded=1 WHERE id=?", (lid,))
        self.conn.commit()

    def get_unuploaded_locations(self):
        cur = self.conn.cursor()
        cur.execute('''
            SELECT id, phone, plate, timestamp, latitude, longitude, address_alias
            FROM locations WHERE uploaded=0
        ''')
        return cur.fetchall()


# ---------- 配置管理 ----------
class ConfigManager:
    def __init__(self, db):
        self.db = db
        # 确保初始配置
        if not self.db.get_config("phone"):
            self.db.set_config("phone", "13800000000")
        if not self.db.get_config("default_plate"):
            self.db.set_config("default_plate", "京A12345")
        if not self.db.get_config("driver_name"):
            self.db.set_config("driver_name", "驾驶员")
        if not self.db.get_config("collect_interval"):
            self.db.set_config("collect_interval", "30")
        if not self.db.get_config("precision_digits"):
            self.db.set_config("precision_digits", "6")
        if not self.db.get_config("server_url"):
            self.db.set_config("server_url", "http://192.168.1.100:5000/upload")
        if not self.db.get_config("server_table"):
            self.db.set_config("server_table", "locations")
        if not self.db.get_config("upload_interval"):
            self.db.set_config("upload_interval", "60")
        # 当前使用的车牌号（可切换）
        if not self.db.get_config("current_plate"):
            self.db.set_config("current_plate", self.db.get_config("default_plate"))

    @property
    def phone(self):
        return self.db.get_config("phone")

    @phone.setter
    def phone(self, val):
        self.db.set_config("phone", val)

    @property
    def current_plate(self):
        return self.db.get_config("current_plate")

    @current_plate.setter
    def current_plate(self, val):
        self.db.set_config("current_plate", val)

    @property
    def driver_name(self):
        return self.db.get_config("driver_name")

    @property
    def collect_interval(self):
        return int(self.db.get_config("collect_interval"))

    @property
    def precision_digits(self):
        return int(self.db.get_config("precision_digits"))

    @property
    def server_url(self):
        return self.db.get_config("server_url")

    @property
    def server_table(self):
        return self.db.get_config("server_table")

    @property
    def upload_interval(self):
        return int(self.db.get_config("upload_interval"))


# ---------- GPS 定位服务 ----------
class GPSService:
    def __init__(self, on_location_callback, on_error_callback):
        self.on_location = on_location_callback
        self.on_error = on_error_callback
        self.requesting = False
        if platform == "android":
            from android.permissions import request_permissions, Permission
            request_permissions([Permission.ACCESS_FINE_LOCATION, Permission.ACCESS_COARSE_LOCATION])

    def request_location(self, timeout=15):
        """请求一次GPS定位，超时则报错"""
        if self.requesting:
            return
        self.requesting = True
        try:
            gps.configure(on_location=self._on_location, on_status=self._on_status)
            gps.start()
            Clock.schedule_once(self._timeout, timeout)
        except Exception as e:
            self.requesting = False
            self.on_error(str(e))

    def _on_location(self, **kwargs):
        if not self.requesting:
            return
        self.requesting = False
        gps.stop()
        lat = kwargs.get('lat')
        lon = kwargs.get('lon')
        if lat is not None and lon is not None:
            self.on_location(lat, lon)
        else:
            self.on_error("No location data")

    def _on_status(self, stype, status):
        pass

    def _timeout(self, dt):
        if self.requesting:
            self.requesting = False
            gps.stop()
            self.on_error("GPS timeout")


# ---------- 界面：主界面 ----------
class MainScreen(Screen):
    def __init__(self, app, **kwargs):
        super().__init__(**kwargs)
        self.app = app
        layout = BoxLayout(orientation='vertical')

        # 顶部信息栏
        self.top_bar = BoxLayout(size_hint_y=0.2)
        self.date_label = Label(text="日期: -")
        self.plate_label = Label(text="车牌: -")
        self.phone_label = Label(text="手机: -")
        self.driver_label = Label(text="司机: -")
        self.top_bar.add_widget(self.date_label)
        self.top_bar.add_widget(self.plate_label)
        self.top_bar.add_widget(self.phone_label)
        self.top_bar.add_widget(self.driver_label)
        layout.add_widget(self.top_bar)

        # 中间按钮区域
        btn_layout = BoxLayout(orientation='vertical', spacing=10, padding=10)
        btn_layout.add_widget(Button(text="切换车牌号", on_press=self.switch_plate))
        btn_layout.add_widget(Button(text="开始", on_press=self.start_collect))
        btn_layout.add_widget(Button(text="结束", on_press=self.stop_collect))
        btn_layout.add_widget(Button(text="数据记录", on_press=self.show_records))
        btn_layout.add_widget(Button(text="数据上传", on_press=self.upload_data))
        btn_layout.add_widget(Button(text="设置", on_press=self.open_settings))
        layout.add_widget(btn_layout)

        self.add_widget(layout)
        self.update_top_display()
        # 定时刷新日期和时间
        Clock.schedule_interval(self.update_top_display, 1)

    def update_top_display(self, *args):
        cfg = self.app.config
        self.date_label.text = f"日期: {datetime.now().strftime('%Y-%m-%d')}"
        self.plate_label.text = f"车牌: {cfg.current_plate}"
        self.phone_label.text = f"手机: {cfg.phone}"
        self.driver_label.text = f"司机: {cfg.driver_name}"

    def switch_plate(self, instance):
        content = BoxLayout(orientation='vertical')
        ti = TextInput(text=self.app.config.current_plate, hint_text="新车牌号")
        content.add_widget(ti)
        popup = Popup(title="输入新车牌号", content=content, size_hint=(0.8, 0.3))
        btn = Button(text="确认", on_press=lambda x: self.do_switch(ti.text, popup))
        content.add_widget(btn)
        popup.open()

    def do_switch(self, new_plate, popup):
        if new_plate.strip():
            self.app.config.current_plate = new_plate.strip()
            self.update_top_display()
        popup.dismiss()

    def start_collect(self, instance):
        self.app.start_tracking()

    def stop_collect(self, instance):
        self.app.stop_tracking()

    def show_records(self, instance):
        self.app.sm.current = 'records'

    def upload_data(self, instance):
        self.app.upload_all_data()

    def open_settings(self, instance):
        self.app.sm.current = 'settings'


# ---------- 数据记录查看界面 ----------
class RecordRecycleView(RecycleView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.data = []
        self.viewclass = 'RecordLabel'
        layout = RecycleBoxLayout(default_size=(None, 40), default_size_hint=(1, None),
                                  orientation='vertical', spacing=5)
        self.add_widget(layout)
        self.layout_manager = layout


class RecordLabel(RecycleDataViewBehavior, Label):
    index = None
    selected = BooleanProperty(False)
    selectable = BooleanProperty(True)

    def refresh_view_attrs(self, rv, index, data):
        self.index = index
        return super().refresh_view_attrs(rv, index, data)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self.selectable:
            return super().on_touch_down(touch)
        return False


class RecordsScreen(Screen):
    def __init__(self, app, **kwargs):
        super().__init__(**kwargs)
        self.app = app
        layout = BoxLayout(orientation='vertical')
        self.rv = RecordRecycleView()
        layout.add_widget(self.rv)
        btn_back = Button(text="返回主界面", size_hint_y=0.1)
        btn_back.bind(on_press=lambda x: setattr(self.app.sm, 'current', 'main'))
        layout.add_widget(btn_back)
        self.add_widget(layout)

    def on_pre_enter(self):
        # 加载数据
        rows = self.app.db.get_all_locations(self.app.config.phone, self.app.config.current_plate)
        data_list = []
        for row in rows:
            # row: id, phone, plate, timestamp, lat, lon, alias, uploaded
            data_list.append({
                'text': f"{row[3]} | {row[4]:.6f},{row[5]:.6f} | {row[6]} | 已上传" if row[
                    7] else f"{row[3]} | {row[4]:.6f},{row[5]:.6f} | {row[6]} | 未上传"
            })
        self.rv.data = data_list


# ---------- 设置界面 ----------
class SettingsScreen(Screen):
    def __init__(self, app, **kwargs):
        super().__init__(**kwargs)
        self.app = app
        layout = BoxLayout(orientation='vertical', spacing=5, padding=10)
        # 手机号
        self.phone_input = TextInput(text=app.config.phone, hint_text="手机号")
        layout.add_widget(Label(text="手机号:"))
        layout.add_widget(self.phone_input)
        # 驾驶员姓名
        self.driver_input = TextInput(text=app.config.driver_name, hint_text="驾驶员姓名")
        layout.add_widget(Label(text="驾驶员姓名:"))
        layout.add_widget(self.driver_input)
        # 默认车牌号（下次切换时的默认）
        self.default_plate_input = TextInput(text=app.db.get_config("default_plate"), hint_text="默认车牌号")
        layout.add_widget(Label(text="默认车牌号:"))
        layout.add_widget(self.default_plate_input)
        # 采集间隔(秒)
        self.collect_interval_input = TextInput(text=str(app.config.collect_interval), hint_text="采集间隔(秒)")
        layout.add_widget(Label(text="采集间隔(秒):"))
        layout.add_widget(self.collect_interval_input)
        # 坐标保留小数位数
        self.precision_input = TextInput(text=str(app.config.precision_digits), hint_text="坐标小数位数")
        layout.add_widget(Label(text="坐标小数位数:"))
        layout.add_widget(self.precision_input)
        # 服务器地址
        self.server_url_input = TextInput(text=app.config.server_url, hint_text="服务器地址")
        layout.add_widget(Label(text="服务器地址:"))
        layout.add_widget(self.server_url_input)
        # 服务器表名
        self.table_name_input = TextInput(text=app.config.server_table, hint_text="数据库表名")
        layout.add_widget(Label(text="数据库表名:"))
        layout.add_widget(self.table_name_input)
        # 上传间隔(秒) - 自动上传
        self.upload_interval_input = TextInput(text=str(app.config.upload_interval), hint_text="自动上传间隔(秒)")
        layout.add_widget(Label(text="自动上传间隔(秒):"))
        layout.add_widget(self.upload_interval_input)

        # 常用地址管理
        layout.add_widget(Label(text="常用地址管理:"))
        self.addr_list_layout = BoxLayout(orientation='vertical')
        layout.add_widget(self.addr_list_layout)
        btn_add_addr = Button(text="添加常用地址", size_hint_y=0.1)
        btn_add_addr.bind(on_press=self.add_address_popup)
        layout.add_widget(btn_add_addr)

        btn_save = Button(text="保存设置", size_hint_y=0.1)
        btn_save.bind(on_press=self.save_settings)
        layout.add_widget(btn_save)
        btn_back = Button(text="返回主界面", size_hint_y=0.1)
        btn_back.bind(on_press=lambda x: setattr(self.app.sm, 'current', 'main'))
        layout.add_widget(btn_back)

        self.add_widget(layout)
        self.refresh_address_list()

    def refresh_address_list(self):
        self.addr_list_layout.clear_widgets()
        addresses = self.app.db.get_all_addresses()
        for addr in addresses:
            line = BoxLayout(size_hint_y=0.08)
            line.add_widget(Label(
                text=f"{addr['alias']} ({addr['min_lat']},{addr['min_lon']})~({addr['max_lat']},{addr['max_lon']})"))
            btn_del = Button(text="删除", size_hint_x=0.2)
            btn_del.bind(on_press=lambda x, aid=addr['id']: self.delete_address(aid))
            line.add_widget(btn_del)
            self.addr_list_layout.add_widget(line)

    def delete_address(self, addr_id):
        self.app.db.delete_address(addr_id)
        self.refresh_address_list()

    def add_address_popup(self, instance):
        popup_layout = BoxLayout(orientation='vertical', spacing=5, padding=10)
        alias = TextInput(hint_text="地址简称")
        min_lat = TextInput(hint_text="最小纬度")
        max_lat = TextInput(hint_text="最大纬度")
        min_lon = TextInput(hint_text="最小经度")
        max_lon = TextInput(hint_text="最大经度")
        speech = TextInput(hint_text="语音播报文本")
        popup_layout.add_widget(Label(text="地址简称:"))
        popup_layout.add_widget(alias)
        popup_layout.add_widget(Label(text="纬度范围:"))
        popup_layout.add_widget(min_lat)
        popup_layout.add_widget(max_lat)
        popup_layout.add_widget(Label(text="经度范围:"))
        popup_layout.add_widget(min_lon)
        popup_layout.add_widget(max_lon)
        popup_layout.add_widget(Label(text="语音播报文本:"))
        popup_layout.add_widget(speech)
        btn_ok = Button(text="确认")
        popup = Popup(title="添加常用地址", content=popup_layout, size_hint=(0.9, 0.7))
        btn_ok.bind(on_press=lambda x: self.save_new_address(
            alias.text, min_lat.text, max_lat.text, min_lon.text, max_lon.text, speech.text, popup))
        popup_layout.add_widget(btn_ok)
        popup.open()

    def save_new_address(self, alias, min_lat, max_lat, min_lon, max_lon, speech, popup):
        try:
            mlat = float(min_lat)
            xlat = float(max_lat)
            mlon = float(min_lon)
            xlon = float(max_lon)
            if alias.strip() and mlat < xlat and mlon < xlon:
                self.app.db.add_address(alias.strip(), mlat, xlat, mlon, xlon, speech.strip())
                self.refresh_address_list()
                popup.dismiss()
            else:
                self.show_error("范围无效")
        except:
            self.show_error("输入数字错误")

    def show_error(self, msg):
        popup = Popup(title="错误", content=Label(text=msg), size_hint=(0.6, 0.3))
        popup.open()

    def save_settings(self, instance):
        self.app.db.set_config("phone", self.phone_input.text.strip())
        self.app.db.set_config("driver_name", self.driver_input.text.strip())
        self.app.db.set_config("default_plate", self.default_plate_input.text.strip())
        self.app.db.set_config("collect_interval", self.collect_interval_input.text.strip())
        self.app.db.set_config("precision_digits", self.precision_input.text.strip())
        self.app.db.set_config("server_url", self.server_url_input.text.strip())
        self.app.db.set_config("server_table", self.table_name_input.text.strip())
        self.app.db.set_config("upload_interval", self.upload_interval_input.text.strip())
        # 如果当前车牌号为空，设为默认
        if not self.app.config.current_plate:
            self.app.config.current_plate = self.app.db.get_config("default_plate")
        popup = Popup(title="提示", content=Label(text="设置已保存，部分参数将在下次采集时生效"), size_hint=(0.6, 0.3))
        popup.open()


# ---------- 主App ----------
class TrackerApp(App):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.db = LocalDB()
        self.config = ConfigManager(self.db)
        self.gps_service = GPSService(self.on_gps_location, self.on_gps_error)
        self.tracking = False
        self.collect_event = None
        self.upload_event = None

    def build(self):
        self.sm = ScreenManager()
        main_screen = MainScreen(self, name='main')
        records_screen = RecordsScreen(self, name='records')
        settings_screen = SettingsScreen(self, name='settings')
        self.sm.add_widget(main_screen)
        self.sm.add_widget(records_screen)
        self.sm.add_widget(settings_screen)
        self.sm.current = 'main'

        # 自动上传定时器
        self.start_auto_upload_timer()
        return self.sm

    def start_auto_upload_timer(self):
        if self.upload_event:
            self.upload_event.cancel()
        self.upload_event = Clock.schedule_interval(self.auto_upload, self.config.upload_interval)

    def auto_upload(self, dt):
        self.upload_all_data()

    def start_tracking(self):
        if self.tracking:
            return
        self.tracking = True
        self.collect_event = Clock.schedule_interval(self.collect_location, self.config.collect_interval)
        # 立即采集一次
        Clock.schedule_once(lambda dt: self.collect_location(None), 0)

    def stop_tracking(self):
        if self.collect_event:
            self.collect_event.cancel()
            self.collect_event = None
        self.tracking = False

    def collect_location(self, dt):
        if not self.tracking:
            return
        self.gps_service.request_location()

    def on_gps_location(self, lat, lon):
        # 舍入精度
        digits = self.config.precision_digits
        lat_rounded = round(lat, digits)
        lon_rounded = round(lon, digits)

        # 判断是否与上一条记录相同
        latest = self.db.get_latest_location(self.config.phone, self.config.current_plate)
        if latest:
            last_lat, last_lon = latest
            if abs(last_lat - lat_rounded) < 1e-9 and abs(last_lon - lon_rounded) < 1e-9:
                # 位置未变化
                return

        # 匹配常用地址
        address_alias = ""
        speech_text = ""
        addresses = self.db.get_all_addresses()
        for addr in addresses:
            if (addr['min_lat'] <= lat <= addr['max_lat'] and
                    addr['min_lon'] <= lon <= addr['max_lon']):
                address_alias = addr['alias']
                speech_text = addr['speech_text']
                break

        # 存入数据库
        self.db.insert_location(self.config.phone, self.config.current_plate,
                                lat_rounded, lon_rounded, address_alias)

        # 语音播报
        if speech_text:
            tts.speak(speech_text)

    def on_gps_error(self, error_msg):
        print("GPS Error:", error_msg)

    def upload_all_data(self):
        unuploaded = self.db.get_unuploaded_locations()
        if not unuploaded:
            return
        server_url = self.config.server_url
        if not server_url:
            return
        uploaded_ids = []
        for row in unuploaded:
            lid, phone, plate, ts, lat, lon, alias = row
            data = {
                "table_name": self.config.server_table,
                "phone": phone,
                "plate": plate,
                "timestamp": ts,
                "latitude": lat,
                "longitude": lon,
                "address_alias": alias
            }
            try:
                resp = requests.post(server_url, json=data, timeout=10)
                if resp.status_code == 200:
                    uploaded_ids.append(lid)
            except Exception as e:
                print("Upload failed:", e)
        if uploaded_ids:
            self.db.mark_uploaded(uploaded_ids)
            # 刷新记录界面（如果正在显示）
            if hasattr(self.sm, 'current_screen') and self.sm.current == 'records':
                self.sm.get_screen('records').on_pre_enter()

    def on_stop(self):
        self.stop_tracking()
        if self.upload_event:
            self.upload_event.cancel()
        self.db.conn.close()


if __name__ == '__main__':
    TrackerApp().run()


