# (C) 2024-2026 ghzserg https://github.com/ghzserg/zmod
# Юнит AFC (ArmoredTurtle AFC-Klipper-Add-On, копия в /usr/data/zmod/zmod/AFC) для IFS AD5X.
# Работает при afc = 2 без родного экрана (fix_config.sh ссылает модули AFC в klippy/extras).
#
# IFS ведёт zmod_ifs/zmod_color: юнит отдаёт загрузку, выгрузку и смену прутка их командам
# (хуки unit_load_lane / unit_unload_lane, как у OpenAMS), а наличие прутка в слотах, пруток
# в голове, цвет и тип берёт из zmod_color.get_status().
# Смена в печати - _A_CHANGE_FILAMENT (подъём, корзина, температура, вентилятор, возврат),
# слот задаёт AFC: перед вызовом карта FILE_CONFIG получает T(n) -> слот lane.
# Карта T(n) -> слот (FILE_CONFIG: диалог цветов перед печатью, ANALOG_PRUTOK) и карта AFC
# (SET_MAP, панель AFC) держатся одинаковыми в обе стороны.
# GPLv3, как AFC.

import json
import os
import traceback
from configparser import Error as error

try: from extras.AFC_utils import ERROR_STR
except: raise error("Error when trying to import AFC_utils.ERROR_STR\n{trace}".format(trace=traceback.format_exc()))

try: from extras.AFC_lane import AFCLaneState
except: raise error(ERROR_STR.format(import_lib="AFC_lane", trace=traceback.format_exc()))

try: from extras.AFC_unit import afcUnit
except: raise error(ERROR_STR.format(import_lib="AFC_unit", trace=traceback.format_exc()))

try: from extras.zmod_color import atomic_write, FILE_CONFIG
except: raise error("AFC_IFS: zmod_color not found\n{trace}".format(trace=traceback.format_exc()))

import extras.AFC_lane as _afc_lane
import extras.AFC_unit as _afc_unit

IFS_TYPE = 'IFS'
SPOOL_WAIT = 10.       # сек на загрузку катушки из Spoolman после SET_SPOOL_ID

# Поведение юнитов AFC задано списками типов: IFS как OpenAMS - датчик на lane, без мотора
for _lst in (_afc_lane.ONLY_LOAD_TYPES, _afc_lane.EXCLUDE_TYPES, _afc_unit.SENSORLESS_UNITS):
    if IFS_TYPE not in _lst:
        _lst.append(IFS_TYPE)


def color_hex(value):
    """'#RRGGBB' (верхний регистр) или ''."""
    v = str(value or '').strip().lstrip('#')
    return '#' + v[:6].upper() if len(v) >= 6 and all(c in '0123456789abcdefABCDEF' for c in v[:6]) else ''


def spool_id(value):
    try:
        sid = int(value)
    except (TypeError, ValueError):
        return None
    return sid if sid > 0 else None


class afcIFS(afcUnit):
    def __init__(self, config):
        super().__init__(config)
        self.type = config.get('type', IFS_TYPE)
        self.stepperless_drive = True
        self.poll_interval = config.getfloat('poll_interval', 1., minval=.2)
        # Опросов подряд с тем же значением, прежде чем AFC узнает о смене (IFS мигает после старта)
        self.stable_polls = config.getint('stable_polls', 3, minval=1)
        self.empty_polls = config.getint('empty_polls', 10, minval=1)   # пруток вынут - дольше
        self.afc_heats = config.getboolean('afc_heats', False)          # греют команды Zmod
        self.sync_materials = config.getboolean('sync_materials', True)
        self.sync_map = config.getboolean('sync_map', True)
        self.afc_runout = config.getboolean('afc_runout', True)
        self.load_cmd = config.get('load_cmd', 'INSERT_PRUTOK_IFS PRUTOK={slot} TRASH=1')
        # Выгрузка из головы: обрезать и вывести пруток из экструдера, он остаётся в трубке
        # готовым к загрузке (как старый пруток при смене)
        self.unload_cmd = config.get('unload_cmd', 'IFS_REMOVE_CURRENT_PRUTOK')
        # Eject: полная выгрузка - пруток уходит из трубки к слоту, катушку можно снять
        self.eject_cmd = config.get('eject_cmd', 'REMOVE_PRUTOK_IFS PRUTOK={slot}')
        self.swap_cmd = config.get('swap_cmd',
            '_A_CHANGE_FILAMENT CHANNEL={tool} RESTORE_POSITION={restore} RESTORE_TEMP={restore}')

        self.color = None
        self.zifs = None
        self.sdcard = None
        self.save_variables = None
        self._slot = {}           # lane -> слот IFS 1..16
        self._seen = {}           # lane -> наличие прутка, о котором знает AFC
        self._pending = {}        # lane -> (значение, число опросов)
        self._head = None         # (слот в голове, опросов) пока не устоялось
        self._busy = False        # идёт наша команда IFS
        self._swap_from = None    # lane, чья выгрузка вошла в смену
        self._await_empty = set() # lane после Eject: ждём, пока слот опустеет
        self.prep_sample = config.getfloat('prep_sample', 8., minval=0.)  # сек опроса слотов на PREP
        self._prep_seen = None    # (до, {слот: был ли пруток}) - выборка PREP
        self._synced = {}         # слот -> (тип, '#RRGGBB'), на которых сошлись обе стороны
        self._map_mtime = None
        self._map_agreed = None   # карта FILE_CONFIG, на которой сошлись с AFC
        self._spool_wait = {}
        self._seeded = False
        self._runout_macro_dropped = False
        self.logo = '<span class=success--text>IFS ready</span>\n'
        self.logo_error = '<span class=error--text>IFS not ready</span>\n'
        # Раньше обработчика AFC_spool (тот регистрируется в своём klippy:connect)
        self.printer.register_event_handler('afc_stepper:register_macros', self._drop_runout_macro)
        self.gcode.register_command('_AFC_IFS_SYNC', self.cmd_AFC_IFS_SYNC,
                                    desc='AFC_IFS: carry the Zmod tool map (FILE_CONFIG) into AFC now')
        self._prepare_varfile(config)

    def _prepare_varfile(self, config):
        # PREP AFC падает без AFC.var.unit (его создаёт install-afc.sh)
        try:
            var = config.getsection('AFC').get('VarFile', None, note_valid=False)
        except Exception:
            var = None
        if not var:
            return
        var = os.path.expanduser(var)
        try:
            os.makedirs(os.path.dirname(var), exist_ok=True)
            if not os.path.exists(var + '.unit'):
                with open(var + '.unit', 'w') as f:
                    f.write('{}')
        except OSError as e:
            self.logger.info("AFC_IFS: %s: %s" % (var, e))

    def _drop_runout_macro(self, lane):
        # Макрос SET_RUNOUT Zmod (заглушка панели AFC) мешает mux-команде AFC
        if self._runout_macro_dropped:
            return
        self._runout_macro_dropped = True
        if 'SET_RUNOUT' not in getattr(self.gcode, 'mux_commands', {}):
            self.gcode.register_command('SET_RUNOUT', None)

    # -- запуск ---------------------------------------------------------------

    def handle_connect(self):
        super().handle_connect()
        self.color = self.printer.lookup_object('zmod_color')
        self.zifs = self.printer.lookup_object('zmod_ifs', None)
        self.sdcard = self.printer.lookup_object('virtual_sdcard', None)
        self.save_variables = self.printer.lookup_object('save_variables', None)
        if not self.afc_heats:
            self._skip_afc_preheat()
        for lane in self.lanes.values():
            slot = getattr(lane, 'index', 0)
            if not 1 <= slot <= 16:
                raise error("[%s]: unit must be %s:<slot>, e.g. unit: %s:1"
                            % (lane.fullname, self.name, self.name))
            self._slot[lane.name] = slot
            # Датчик слота ведёт опрос; у IFS нет датчика хаба (без load pin AFC_lane считает его True)
            lane.prep_state = False
            lane._load_state = False
            lane.loaded_to_hub = False

    def _skip_afc_preheat(self):
        # AFC греет и ждёт перед каждой загрузкой/выгрузкой; Zmod греет сам под материал
        afc = self.afc
        if getattr(afc, '_ifs_preheat_wrapped', False):
            return
        orig = afc._check_extruder_temp
        def check(cur_lane, *args, **kwargs):
            unit = getattr(cur_lane, 'unit_obj', None)
            if isinstance(unit, afcIFS) and not unit.afc_heats:
                return False
            return orig(cur_lane, *args, **kwargs)
        afc._check_extruder_temp = check
        afc._ifs_preheat_wrapped = True

    def handle_ready(self):
        super().handle_ready()
        self.reactor.register_timer(self._poll_safe, self.reactor.monotonic() + self.poll_interval)

    # -- состояние Zmod ---------------------------------------------------------

    def _status(self, eventtime=None):
        try:
            return self.color.get_status(eventtime or self.reactor.monotonic())
        except Exception:
            self.logger.debug("AFC_IFS: zmod_color.get_status failed\n" + traceback.format_exc())
            return {}

    def _ifs_on(self, st):
        # Связь с IFS - у zmod_ifs (zmod_color.ifs появляется только после диалога цветов)
        return bool(getattr(self.zifs, 'ifs', False)) if self.zifs is not None else bool(st.get('ifs'))

    @staticmethod
    def _slots(st):
        return {int(s['ID']): s for s in st.get('slots') or []}

    def _present(self, st, lane):
        return bool(self._slots(st).get(self._slot[lane.name], {}).get('hasFilament'))

    @staticmethod
    def _in_head(st):
        # FFMInfo channel остаётся после выгрузки: пруток в голове - только с датчиком
        return int(st.get('channel') or 0) if st.get('extruder_sensor') else 0

    def _lane_for_slot(self, slot):
        for name, s in self._slot.items():
            if s == slot:
                return self.lanes.get(name)
        return None

    def _set_presence(self, lane, present):
        lane.prep_state = present
        lane.loaded_to_hub = present
        lane._load_state = False

    def _prepped(self):
        return bool(self.lanes) and all(getattr(l, '_afc_prep_done', False) for l in self.lanes.values())

    def _send_lane_data(self, lane):
        # Клиент Moonraker у AFC появляется только на PREP
        if getattr(self.afc, 'moonraker', None) is not None:
            lane.send_lane_data()

    def _poll_safe(self, eventtime):
        # Исключение в таймере reactor останавливает Klipper
        try:
            return self._poll(eventtime)
        except Exception:
            self.logger.error("AFC_IFS: poll failed\n%s" % traceback.format_exc())
            return eventtime + max(self.poll_interval, 5.)

    def _poll(self, eventtime):
        if self._busy or not self._prepped() or self.afc.current_state not in ('Idle', None):
            return eventtime + self.poll_interval
        st = self._status(eventtime)
        if not st or not self._ifs_on(st):
            return eventtime + self.poll_interval
        head = bool(st.get('extruder_sensor'))
        for lane in self.lanes.values():
            if lane.extruder_obj is not None and lane.extruder_obj.tool_start is None:
                lane.extruder_obj.tool_start_state = head

        for lane in self.lanes.values():
            now = self._present(st, lane)
            seen = self._seen.get(lane.name)
            if seen is None:
                self._seen[lane.name] = now
                self._set_presence(lane, now)
                continue
            if not now:
                self._await_empty.discard(lane.name)
            if now == seen:
                self._pending.pop(lane.name, None)
                if (now and not lane.load_state and not lane.tool_loaded
                        and lane.name not in self._await_empty
                        and lane.status != AFCLaneState.EJECTING):
                    # Пруток в слоте, а AFC считает lane пустой (после Eject вынули и вставили
                    # быстрее empty_polls): вставка для AFC
                    self.logger.info("AFC_IFS: %s has filament again" % lane.name)
                    self._set_presence(lane, True)
                    try:
                        lane.handle_load_runout(eventtime, True)
                    except Exception:
                        self.logger.error("AFC_IFS: %s insert failed: %s" % (lane.name, traceback.format_exc()))
                continue
            value, count = self._pending.get(lane.name, (now, 0))
            count = count + 1 if value == now else 1
            if count < (self.stable_polls if now else self.empty_polls):
                self._pending[lane.name] = (now, count)
                continue
            self._pending.pop(lane.name, None)
            self._seen[lane.name] = now
            self._set_presence(lane, now)
            if lane.status == AFCLaneState.EJECTING and not now:
                lane.status = AFCLaneState.NONE
            try:
                # Вставка/окончание прутка по логике AFC: set_loaded, катушка, бесконечная катушка/пауза
                lane.handle_load_runout(eventtime, now)
            except Exception:
                self.logger.error("AFC_IFS: %s sensor change failed\n%s" % (lane.name, traceback.format_exc()))

        self._sync_tool(self._in_head(st))
        if self.sync_map:
            self._sync_map()
        if self.sync_materials:
            self._sync_materials(st)
        self._seed_spools(eventtime)
        return eventtime + self.poll_interval

    # -- пруток в голове: Zmod -> AFC -------------------------------------------

    def _sync_tool(self, slot, now=False):
        """Lane в голове у AFC = слот в голове у Zmod (смена мимо AFC: меню Zmod, ANALOG_PRUTOK)."""
        if not now:
            last, count = self._head or (slot, 0)
            count = count + 1 if last == slot else 1
            self._head = (slot, count)
            if count < self.stable_polls:
                return
        want = self._lane_for_slot(slot) if slot else None
        have = None
        for lane in self.lanes.values():
            if lane.tool_loaded or (lane.extruder_obj is not None and lane.extruder_obj.lane_loaded == lane.name):
                have = lane
                break
        if want is have:
            return
        if have is not None:
            self.logger.info("AFC_IFS: %s left the toolhead outside AFC" % have.name)
            have.set_tool_unloaded(normal_toolchange=True)
            if have.prep_state:
                have.status = AFCLaneState.LOADED
        if want is not None:
            self.logger.info("AFC_IFS: %s is in the toolhead (Zmod slot %d)" % (want.name, slot))
            want.loaded_to_hub = True
            want.set_tool_loaded(normal_toolchange=True)
        self.afc.save_vars()

    # -- карта T(n) -> слот: FILE_CONFIG <-> AFC ---------------------------------

    def _read_map(self):
        try:
            mtime = os.stat(FILE_CONFIG).st_mtime
            with open(FILE_CONFIG, 'r') as f:
                return mtime, [int(x) for x in json.load(f)]
        except Exception:
            return None, None

    def _afc_map(self, length):
        """FILE_CONFIG по карте AFC: T(n) -> слот его lane (T без lane IFS - как в файле)."""
        out = []
        for t in range(length):
            lane = self.lanes.get(self.afc.tool_cmds.get('T%d' % t, ''))
            out.append(self._slot[lane.name] if lane is not None else None)
        return out

    def _sync_map(self, run=None):
        mtime, fmap = self._read_map()
        if fmap is None:
            return
        tools = [int(k[1:]) for k in self.afc.tool_cmds if k[1:].isdigit()]
        length = max([len(fmap)] + [t + 1 for t in tools])
        amap = self._afc_map(length)
        merged = [a if a is not None else (fmap[t] if t < len(fmap) else None) for t, a in enumerate(amap)]
        if mtime != self._map_mtime or self._map_agreed is None:
            # Файл изменился (диалог цветов, ANALOG_PRUTOK) или первый взгляд: файл -> AFC
            self._map_mtime = mtime
            self._map_agreed = list(fmap)
            self._apply_file_map(fmap, run or self.gcode.run_script)
        elif merged[:len(fmap)] != fmap or len(merged) > len(fmap):
            # Изменилось в AFC (SET_MAP, панель, бесконечная катушка): AFC -> файл
            new = [m if m is not None else (t % len(self._slot)) + 1 for t, m in enumerate(merged)]
            if new != fmap:
                atomic_write(FILE_CONFIG, json.dumps(new, indent=2))
                self._map_mtime, _ = self._read_map()
                self._map_agreed = new

    def _apply_file_map(self, fmap, run):
        if not getattr(self.afc, 'enable_multiple_mapping', False):
            self.logger.info("AFC_IFS: sync_map needs [AFC] enable_multiple_mapping: True")
            return
        cmds = []
        for t, slot in enumerate(fmap):
            lane = self._lane_for_slot(slot)
            tcmd = 'T%d' % t
            if lane is None or self.afc.tool_cmds.get(tcmd) == lane.name:
                continue
            if tcmd in self.afc.tool_cmds:
                cmds.append('SET_MAP LANE=%s MAP=%s' % (lane.name, tcmd))
            else:
                cmds.append('AFC_ADD_MAPPING LANE=%s MAPPING=%s' % (lane.name, tcmd))
        if cmds:
            self.logger.info("AFC_IFS: following the Zmod tool map: %s" % ', '.join(cmds))
        for cmd in cmds:
            try:
                run(cmd)
            except Exception as e:
                self.logger.info("AFC_IFS: %s failed: %s" % (cmd, e))

    def cmd_AFC_IFS_SYNC(self, gcmd):
        # Перед T(n) печати и первой загрузкой: карта диалога цветов уже в AFC
        if self._prepped() and self.sync_map:
            self._sync_map(self.gcode.run_script_from_command)

    # -- тип и цвет: lane AFC <-> слот Zmod -------------------------------------

    def _zmod_type(self, mat):
        types = [t for t in self.color.valid_types[:-1]]
        mat = (mat or '').upper()
        if mat in types:
            return mat
        return next((t for t in sorted(types, key=len, reverse=True) if t and t in mat), None)   # PLA+ -> PLA

    def _apply_zmod(self, lane, s):
        mat = s.get('Material') or ''
        lane.material = '' if mat in ('', '?') else mat
        hexc = color_hex(s.get('HEX'))
        if hexc:
            lane.color = hexc
        if lane.spool_id is None and not lane.weight:
            lane.weight = 1000      # как AFC_spool без Spoolman: панели рисуют 0 г пустой катушкой
        self._send_lane_data(lane)

    def _push_zmod(self, lane, mat, hexc):
        slot = self._slot[lane.name]
        s = self._slots(self._status()).get(slot, {})
        zmat = self._zmod_type(mat) or s.get('Material') or 'PLA'
        zhex = (hexc or color_hex(s.get('HEX')) or '#FFFFFF')
        def run(eventtime):
            try:
                code, resp = self.color.set_printer_data_detail(slot, zmat, zhex)
                if code != 200:
                    self.logger.info("AFC_IFS: slot %d %s %s: %s" % (slot, zmat, zhex, resp))
            except Exception as e:
                self.logger.info("AFC_IFS: slot %d %s %s: %s" % (slot, zmat, zhex, e))
        self.reactor.register_callback(run)
        return zmat, zhex

    def _sync_materials(self, st):
        slots = self._slots(st)
        changed = False
        for lane in self.lanes.values():
            slot = self._slot[lane.name]
            s = slots.get(slot)
            if s is None or not lane.prep_state:
                self._synced.pop(slot, None)
                continue
            zm = s.get('Material') or ''
            zmod = ('' if zm in ('', '?') else zm, color_hex(s.get('HEX')))
            afc = (lane.material or '', color_hex(lane.color))
            agreed = self._synced.get(slot)
            if agreed is None or zmod != agreed or afc == ('', ''):
                # Первый взгляд (Zmod прав), изменили в Zmod (меню цветов, NFC) или AFC стёр lane
                if afc != zmod:
                    self._apply_zmod(lane, s)
                    changed = True
                self._synced[slot] = zmod
            elif afc != agreed:
                # Изменили в AFC: SET_COLOR, SET_MATERIAL, SET_SPOOL_ID
                zmat, zhex = self._push_zmod(lane, afc[0], afc[1])
                self._synced[slot] = (zmat, zhex)
        if changed:
            self.afc.save_vars()

    # -- катушки встроенного zmod_afc (afc_spool_laneN) -> AFC, один раз --------

    def _seed_spools(self, eventtime):
        if self._seeded or getattr(self.afc, 'spoolman', None) is None or self.save_variables is None:
            return
        self._seeded = True
        v = self.save_variables.allVariables
        if v.get('afc_full_seeded', 0):
            return
        scripts = []
        for lane in self.lanes.values():
            sid = spool_id(v.get('afc_spool_lane%d' % self._slot[lane.name]))
            if sid is not None and spool_id(lane.spool_id) is None and lane.prep_state:
                self.logger.info("AFC_IFS: %s keeps spool %s from the built-in AFC" % (lane.name, sid))
                scripts.append('SET_SPOOL_ID LANE=%s SPOOL_ID=%d' % (lane.name, sid))
        scripts.append('SAVE_VARIABLE VARIABLE=afc_full_seeded VALUE=1')
        def run(e):
            for script in scripts:
                try:
                    self.gcode.run_script(script)
                except Exception as ex:
                    self.logger.info("AFC_IFS: %s failed: %s" % (script, ex))
        self.reactor.register_callback(run)

    # -- PREP --------------------------------------------------------------------

    def system_Test(self, cur_lane, delay, assignTcmd, enable_movement):
        st = self._status()
        # IFS отвечает через несколько секунд после старта: ждём до 30 с
        until = self.reactor.monotonic() + 30.
        while not self._ifs_on(st) and self.reactor.monotonic() < until:
            self.reactor.pause(self.reactor.monotonic() + .5)
            st = self._status()
        succeeded = self._ifs_on(st)
        sample = self._prep_presence() if succeeded else {}
        msg = ''
        if not succeeded:
            msg = '<span class=error--text>IFS OFF</span>'
        else:
            present = sample.get(self._slot[cur_lane.name], self._present(st, cur_lane))
            self._seen[cur_lane.name] = present
            self._set_presence(cur_lane, present)
            in_head = self._in_head(st) == self._slot[cur_lane.name]
            if in_head and not cur_lane.tool_loaded:
                cur_lane.set_tool_loaded()
            elif cur_lane.tool_loaded and not in_head:
                cur_lane.set_tool_unloaded()
            if present or in_head:
                self.lane_loaded(cur_lane)
                cur_lane.status = AFCLaneState.LOADED
                msg += '<span class=success--text>LOCKED AND LOADED</span>'
                if in_head:
                    cur_lane.loaded_to_hub = True
                    cur_lane.status = AFCLaneState.TOOLED
                    self.afc.spool.set_active_spool(cur_lane.spool_id)
                    self.lane_tool_loaded(cur_lane)
                    msg += '<span class=primary--text> in ToolHead</span>'
                    self.printer.send_event("afc:tool_loaded", cur_lane)
            else:
                if not cur_lane.remember_spool:
                    self.afc.spool.clear_values(cur_lane)
                self.lane_not_ready(cur_lane)
                msg += 'EMPTY READY FOR SPOOL'
        if assignTcmd:
            self.afc.function.TcmdAssign(cur_lane)
        if isinstance(cur_lane.map, tuple):
            # map из конфига - tuple getlist(); SET_MAP с enable_multiple_mapping падает на tuple + list
            cur_lane.map = list(cur_lane.map)
        if succeeded and self.sync_materials and cur_lane.prep_state:
            s = self._slots(st).get(self._slot[cur_lane.name])
            if s is not None:
                self._apply_zmod(cur_lane, s)
                zm = s.get('Material') or ''
                self._synced[self._slot[cur_lane.name]] = ('' if zm in ('', '?') else zm, color_hex(s.get('HEX')))
        self._send_lane_data(cur_lane)     # AFC стёр lane_data в начале PREP
        cur_lane.do_enable(False)
        self.logger.info('{lane_name} tool cmd: {tcmd:3} {msg}'.format(
            lane_name=cur_lane.name, tcmd=cur_lane.map_to_string(), msg=msg))
        cur_lane.set_afc_prep_done()
        return succeeded

    def _prep_presence(self):
        # IFS после старта на секунды показывает слоты пустыми: пустой на PREP стёр бы катушку.
        # Один опрос на все lane PREP: слот с прутком хотя бы в одном чтении - с прутком
        now = self.reactor.monotonic()
        if self._prep_seen is not None and now < self._prep_seen[0]:
            return self._prep_seen[1]
        seen = {}
        until = now + self.prep_sample
        while True:
            for slot, s in self._slots(self._status()).items():
                seen[slot] = seen.get(slot, False) or bool(s.get('hasFilament'))
            if self.reactor.monotonic() >= until:
                break
            self.reactor.pause(self.reactor.monotonic() + .5)
        self._prep_seen = (self.reactor.monotonic() + 5., seen)
        return seen

    # -- движения (всё командами Zmod) -------------------------------------------

    def _tool(self, lane):
        cm = str(getattr(lane, 'current_map', '') or '')
        if cm[1:].isdigit():
            return int(cm[1:])
        for m in lane.map or []:
            if str(m)[1:].isdigit():
                return int(str(m)[1:])
        return self._slot[lane.name] - 1

    def _run(self, template, lane, keep_temp=False, **kw):
        cmd = template.format(slot=self._slot[lane.name], lane=lane.name, **kw)
        self.logger.debug("AFC_IFS: " + cmd)
        self._busy = True
        # Вне печати команды Zmod греют под материал и оставляют сопло горячим: вернуть как было
        heater = self.printer.lookup_object('extruder').get_heater() if keep_temp else None
        target = heater.target_temp if heater is not None else None
        try:
            self.gcode.run_script_from_command(cmd)
            return True, ''
        except Exception as e:
            return False, str(e)
        finally:
            if target is not None and not self.afc.function.in_print():
                self.gcode.run_script_from_command("M104 S%d" % int(target))
            self._busy = False
            self._pending.clear()
            self._head = None

    def _fail_state(self):
        # Вне печати handle_lane_failure не ставит паузу, и AFC остаётся в Loading/Unloading
        if not self.afc.error_state:
            self.afc.error.set_error_state(True)

    def _set_file_tool(self, tool, slot):
        # _CHANGE_FILAMENT берёт слот из FILE_CONFIG[T]: слот выбрал AFC
        _, fmap = self._read_map()
        fmap = list(fmap or [])
        while len(fmap) <= tool:
            fmap.append((len(fmap) % len(self._slot)) + 1)
        if fmap[tool] != slot:
            fmap[tool] = slot
            atomic_write(FILE_CONFIG, json.dumps(fmap, indent=2))
        self._map_mtime, self._map_agreed = self._read_map()

    def unit_load_lane(self, cur_lane, cur_extruder):
        self._swap_from = None
        slot = self._slot[cur_lane.name]
        if self.afc.function.in_print():
            tool = self._tool(cur_lane)
            self._set_file_tool(tool, slot)
            # С T(n) файла печати (virtual_sdcard ждёт смену) - возврат позиции и температуры,
            # первая загрузка в START_PRINT (SET_CURRENT_PRUTOK) - без них
            restore = 1 if getattr(self.sdcard, 'change_filament', False) else 0
            ok, err = self._run(self.swap_cmd, cur_lane, tool=tool, restore=restore)
        else:
            ok, err = self._run(self.load_cmd, cur_lane)
        st = self._status()
        if ok and self._in_head(st) != slot:
            ok, err = False, "Zmod reports slot %s in the toolhead" % (self._in_head(st) or 'none')
        if not ok:
            self.afc.error.handle_lane_failure(
                cur_lane, "IFS load of %s failed: %s" % (cur_lane.name, err),
                pause=self.afc.function.in_print())
            self._fail_state()
            self._sync_tool(self._in_head(st), now=True)
            return False
        cur_lane.loaded_to_hub = True
        cur_lane.status = AFCLaneState.TOOL_LOADED
        self.afc.save_vars()
        return True

    def unit_unload_lane(self, cur_lane, cur_extruder):
        nxt = self.afc.next_lane_load
        if self.afc.in_toolchange and nxt in self._slot and nxt != cur_lane.name:
            # Следом TOOL_LOAD lane nxt: смену целиком делает Zmod (сам вынимает текущий пруток)
            self._swap_from = cur_lane.name
        else:
            self._swap_from = None
            ok, err = self._unload(cur_lane)
            st = self._status()
            if ok and self._in_head(st) == self._slot[cur_lane.name]:
                ok, err = False, "the lane is still in the toolhead"
            if not ok:
                self.afc.error.handle_lane_failure(
                    cur_lane, "IFS unload of %s failed: %s" % (cur_lane.name, err),
                    pause=self.afc.function.in_print())
                self._fail_state()
                return False
        if self.afc.post_unload_macro is not None:
            self.gcode.run_script_from_command(self.afc.post_unload_macro)
        cur_lane.set_tool_unloaded(normal_toolchange=True)
        cur_lane.status = AFCLaneState.LOADED if cur_lane.prep_state else AFCLaneState.NONE
        self.afc.save_vars()
        return True

    def eject_lane(self, lane):
        self._await_empty.add(lane.name)
        # Из головы или из трубки - пруток уходит к слоту IFS, дальше катушку снимают рукой
        ok, err = self._run(self.eject_cmd, lane, keep_temp=True)
        if not ok:
            self.afc.error.AFC_error("IFS eject of %s failed: %s" % (lane.name, err), pause=False)
            return
        self.gcode.respond_info("AFC_IFS: %s - pull the filament out of IFS slot %d"
                                % (lane.name, self._slot[lane.name]))

    def _unload(self, lane):
        return self._run(self.unload_cmd, lane, keep_temp=True)

    def lane_unload(self, cur_lane):
        self.eject_lane(cur_lane)
        return True

    # -- к IFS не относится -------------------------------------------------------

    def prep_load(self, lane):
        pass

    def prep_post_load(self, lane):
        pass

    def lane_move(self, cur_lane, distance, speed_mode):
        self.logger.info("LANE_MOVE is not available on IFS lanes")

    def get_lane_reset_command(self, lane, dis):
        return None

    def calibration_lane_message(self):
        return "The IFS needs no AFC calibration."

    def calibrate_lane(self, cur_lane, tol):
        return False, self.calibration_lane_message(), 0

    def calibrate_bowden(self, cur_lane, dis, tol):
        return False, self.calibration_lane_message(), 0

    def calibrate_hub(self, cur_lane, tol):
        return False, self.calibration_lane_message(), 0

    def calibrate_td1(self, cur_lane, dis, tol):
        return False, "TD-1 is not supported on IFS lanes", 0

    def select_lane(self, lane, sel_prep=False):
        return True, 0

    def return_to_home(self, prep=False):
        return True

    def check_runout(self, cur_lane):
        # Окончание прутка по AFC - только для lane в голове во время печати
        return (self.afc_runout
                and cur_lane.name == self.afc.function.get_current_lane()
                and self.afc.function.is_printing()
                and cur_lane.status not in (AFCLaneState.EJECTING, AFCLaneState.CALIBRATING))


def load_config_prefix(config):
    return afcIFS(config)
