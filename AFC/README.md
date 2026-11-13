# AFC-Klipper-Add-On (vendored)

Copy of `extras/` from [AFCProject/AFC-Klipper-Add-On](https://github.com/AFCProject/AFC-Klipper-Add-On)
at the commit in `AFC_COMMIT`, unchanged. License: GPLv3 (`LICENSE`).

Used when the mod variable `afc = 2` on an AD5X without the stock screen
(`display_off = 1`): `fix_config.sh` / `start.sh` link `extras/*.py` into
`klippy/extras`; with any other value the links are removed and the built-in
emulation `zmod_afc` serves the AFC panel as before.

Update: `./AFC/sync_afc.sh <AFC-Klipper-Add-On clone> [commit]`

The IFS unit, `.shell/AFC_IFS.py`, is the shared AFC unit for Zmod and Kiln OS
(Zmod and Kiln backends, `firmware: auto`), proposed for AFC itself. Until AFC
ships it, Zmod carries a copy; keep it identical to the AFC branch's
`extras/AFC_IFS.py`.

Testers: install, update and rollback steps are in [TESTING.md](TESTING.md).

---

Копия `extras/` из AFC-Klipper-Add-On на коммите из `AFC_COMMIT`, без изменений,
лицензия GPLv3. Работает при `afc = 2` на AD5X без родного экрана; иначе
ссылки удаляются и панель AFC даёт встроенная эмуляция `zmod_afc`.
