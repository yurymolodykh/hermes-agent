Ревизия: `e8ee0fb7a24a647bdcfde5b8c08941e1717bb9bb`. Сгенерировано `durable/t_eca8cb15/probes/route_audit.py`.

Регистраций в `hermes_cli/web_routers/*.py` (38 модулей): **336** = HTTP 325 + `api_route` 1 + WS 10; `async` 301 (уникальных 299), `def` 35. `async`-обработчиков с оффлоадом: **173**.

| Файл:строка | Обработчик | Блокирующие вызовы (строка:вызов) |
|---|---|---|
| `actions.py:386` | `get_action_status` | 436:`proc.wait` |
| `audio.py:90` | `transcribe_audio_upload` | 123:`tmp.write` |
| `files.py:936` | `upload_managed_file_stream` | 946:`target.parent.mkdir` |
| `files.py:1083` | `fs_write_text` | 1139:`tmp.write_text`, 1140:`os.replace`, 1142:`tmp.unlink`, 1145:`tmp.unlink` |
| `local_models.py:796` | `local_models_delete` | 803:`path.unlink` |
| `local_models.py:1041` | `local_models_sideload` | 1050:`dest.parent.mkdir`, 1052:`os.link`, 1055:`os.symlink` |
| `ops.py:601` | `run_backup` | 608:`archive.parent.mkdir` |
| `ops.py:666` | `run_import_upload` | 674:`staging_dir.mkdir`, 685:`target.unlink` |
| `profiles.py:1124` | `open_profile_terminal_endpoint` | 1130:`subprocess.Popen`, 1133:`subprocess.Popen`, 1139:`subprocess.Popen` |

Только метаданные (в порог не входят):

| Файл:строка | Обработчик | Вызовы |
|---|---|---|
| `audio.py:299` | `speak_text` | 328:`os.path.isfile` |
| `dashboard_ui.py:409` | `serve_plugin_asset` | 437:`base.resolve`, 439:`target.exists`, 439:`target.is_file` |
| `files.py:428` | `get_media` | 442:`target.is_file`, 444:`target.stat` |
| `files.py:1389` | `fs_git_root` | 1401:`target.stat` |
| `ops.py:620` | `download_dashboard_backup` | 631:`target.is_file` |
| `ops.py:647` | `run_import` | 652:`os.path.isfile` |
| `profiles.py:1395` | `import_profile_endpoint` | 1419:`overlay_path.is_file` |
| `status.py:998` | `get_logs` | 1008:`log_path.exists` |

Вложенные помощники, уже уходящие в пул по имени:

| Файл:строка | Обработчик | Помощник | Вызовы |
|---|---|---|---|
| `analytics.py:43` | `get_config_raw` | `_run` | 52:`path.read_text`, 50:`path.exists` |
| `audio.py:257` | `get_elevenlabs_voices` | `_fetch` | 258:`urllib.request.urlopen` |
| `files.py:577` | `upload_chat_image` | `_run` | 596:`target.write_bytes` |
| `ops.py:503` | `get_memory_status` | `_run` | 514:`p.stat` |
| `ops.py:554` | `reset_memory` | `_run` | 565:`path.unlink`, 563:`path.exists` |
| `ops.py:830` | `list_checkpoints` | `_run` | 835:`os.scandir`, 841:`child.rglob`, 834:`cp_dir.is_dir`, 838:`child.is_dir`, 842:`f.is_file`, 844:`f.stat` |
| `profiles.py:1199` | `get_profile_soul` | `_run` | 1203:`soul_path.read_text`, 1201:`soul_path.exists` |
| `profiles.py:1256` | `apply_soul_questionnaire` | `_run` | 1261:`soul_path.read_text`, 1261:`soul_path.exists` |
| `skills.py:288` | `scan_skill_hub` | `_run` | 328:`_shutil.rmtree` |
| `skills.py:463` | `get_skill_content` | `_read` | 471:`skill_md.read_text`, 468:`skill_md.exists` |
