import asyncio
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from queue import Queue

import requests as http_requests
from flask_cors import CORS
from myUtils.auth import check_cookie
from flask import Flask, request, jsonify, Response, render_template, send_from_directory
from werkzeug.utils import secure_filename
from conf import BASE_DIR
from sau_agent_pkg.db_init import DB_PATH
from myUtils.login import get_tencent_cookie, douyin_cookie_gen, get_ks_cookie, xiaohongshu_cookie_gen
from myUtils.postVideo import post_video_tencent, post_video_DouYin, post_video_ks, post_video_xhs

# ---------------------------------------------------------------------------
# 日志初始化（带时间戳）
# ---------------------------------------------------------------------------
# 为什么在 sau_backend.py 里显式初始化：该文件里历史遗留了大量 print(...) 裸输出，
# 打包后排查"发布失败发生在几点"/"cookie 检查异常发生在几点"完全无从下手。
# 把格式和其他入口（tray_app / runner.py / service_host.py）统一为 `YYYY-MM-DD HH:mm:ss`，
# 跨模块 grep 排障时时间线能严格对齐。
_BACKEND_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_BACKEND_DATEFMT = "%Y-%m-%d %H:%M:%S"
logging.basicConfig(
    level=logging.INFO,
    format=_BACKEND_FMT,
    datefmt=_BACKEND_DATEFMT,
    force=True,  # 防止被 Nuitka/子进程预配置的空 handler 覆盖
)
# logger name = sau_backend，日志里一眼就能看出是旧 Web 端哪条链路
logger = logging.getLogger("sau_backend")

active_queues = {}
app = Flask(__name__)

#允许所有来源跨域访问
CORS(app)

# 限制上传文件大小为160MB
app.config['MAX_CONTENT_LENGTH'] = 160 * 1024 * 1024

# 获取当前目录（假设 index.html 和 assets 在这里）
current_dir = os.path.dirname(os.path.abspath(__file__))

# 处理所有静态资源请求（未来打包用）
@app.route('/assets/<filename>')
def custom_static(filename):
    return send_from_directory(os.path.join(current_dir, 'assets'), filename)

# 处理 favicon.ico 静态资源（未来打包用）
@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(current_dir, 'assets'), 'vite.svg')

@app.route('/vite.svg')
def vite_svg():
    return send_from_directory(os.path.join(current_dir, 'assets'), 'vite.svg')

# （未来打包用）
@app.route('/')
def index():  # put application's code here
    return send_from_directory(current_dir, 'index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No file part in the request"
        }), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No selected file"
        }), 400
    try:
        # 保存文件到指定位置
        uuid_v1 = uuid.uuid1()
        logger.debug("upload_file UUID v1: %s", uuid_v1)  # 追踪视频文件时可用来关联上传时刻
        safe_name = secure_filename(file.filename)
        if not safe_name:
            return jsonify({"code": 400, "data": None, "msg": "Invalid filename"}), 400
        filepath = Path(BASE_DIR / "videoFile" / f"{uuid_v1}_{safe_name}")
        file.save(filepath)
        return jsonify({"code":200,"msg": "File uploaded successfully", "data": f"{uuid_v1}_{safe_name}"}), 200
    except Exception as e:
        return jsonify({"code":500,"msg": str(e),"data":None}), 500

@app.route('/getFile', methods=['GET'])
def get_file():
    # 获取 filename 参数
    filename = request.args.get('filename')

    if not filename:
        return jsonify({"code": 400, "msg": "filename is required", "data": None}), 400

    # 防止路径穿越攻击
    if '..' in filename or filename.startswith('/'):
        return jsonify({"code": 400, "msg": "Invalid filename", "data": None}), 400

    # 拼接完整路径
    file_path = str(Path(BASE_DIR / "videoFile"))

    # 返回文件
    return send_from_directory(file_path,filename)


@app.route('/uploadSave', methods=['POST'])
def upload_save():
    if 'file' not in request.files:
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No file part in the request"
        }), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({
            "code": 400,
            "data": None,
            "msg": "No selected file"
        }), 400

    # 获取表单中的自定义文件名（可选）
    custom_filename = request.form.get('filename', None)
    if custom_filename:
        filename = secure_filename(custom_filename + "." + file.filename.split('.')[-1])
    else:
        filename = secure_filename(file.filename)
    if not filename:
        return jsonify({"code": 400, "data": None, "msg": "Invalid filename"}), 400

    try:
        # 生成 UUID v1
        uuid_v1 = uuid.uuid1()
        logger.debug("upload_save UUID v1: %s original_filename=%s", uuid_v1, file.filename)

        # 构造文件名和路径
        final_filename = f"{uuid_v1}_{filename}"
        filepath = Path(BASE_DIR / "videoFile" / f"{uuid_v1}_{filename}")

        # 保存文件
        file.save(filepath)

        with sqlite3.connect(str(DB_PATH)) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                                INSERT INTO file_records (filename, filesize, file_path)
            VALUES (?, ?, ?)
                                ''', (filename, round(float(os.path.getsize(filepath)) / (1024 * 1024),2), final_filename))
            conn.commit()
            logger.info("✅ 上传文件已记录到 file_records: %s (%.2f MB)", final_filename,
                        round(float(os.path.getsize(filepath)) / (1024 * 1024),2))

        return jsonify({
            "code": 200,
            "msg": "File uploaded and saved successfully",
            "data": {
                "filename": filename,
                "filepath": final_filename
            }
        }), 200

    except Exception as e:
        logger.error("upload_save 失败: %s", e, exc_info=True)  # 上传失败很容易被用户抱怨，需要完整堆栈+时间戳
        return jsonify({
            "code": 500,
            "msg": f"upload failed: {e}",
            "data": None
        }), 500

@app.route('/getFiles', methods=['GET'])
def get_all_files():
    try:
        # 使用 with 自动管理数据库连接
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row  # 允许通过列名访问结果
            cursor = conn.cursor()

            # 查询所有记录
            cursor.execute("SELECT * FROM file_records")
            rows = cursor.fetchall()

            # 将结果转为字典列表，并提取UUID
            data = []
            for row in rows:
                row_dict = dict(row)
                # 从 file_path 中提取 UUID (文件名的第一部分，下划线前)
                if row_dict.get('file_path'):
                    file_path_parts = row_dict['file_path'].split('_', 1)  # 只分割第一个下划线
                    if len(file_path_parts) > 0:
                        row_dict['uuid'] = file_path_parts[0]  # UUID 部分
                    else:
                        row_dict['uuid'] = ''
                else:
                    row_dict['uuid'] = ''
                data.append(row_dict)

            return jsonify({
                "code": 200,
                "msg": "success",
                "data": data
            }), 200
    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str("get file failed!"),
            "data": None
        }), 500


@app.route("/getAccounts", methods=['GET'])
def getAccounts():
    """快速获取所有账号信息，不进行cookie验证"""
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
            SELECT * FROM user_info''')
            rows = cursor.fetchall()
            rows_list = [list(row) for row in rows]

            print_rows = [list(row) for row in rows]

            # 为什么把 print 替换为 logger.debug：
            # 之前直接 print(📋 当前数据表内容) 属于纯调试输出，带时间戳后能知道什么时候发生的，
            # 又因为每次 getAccounts 都会调用，调试量比较大，故用 debug 级别——
            # 默认 INFO 级别下不会刷屏，用户要排障时才会开到 DEBUG 看。
            logger.debug("📋 当前数据表内容（快速获取）: %s", print_rows)

            return jsonify(
                {
                    "code": 200,
                    "msg": None,
                    "data": rows_list
                }), 200
    except Exception as e:
        logger.error("获取账号列表时出错: %s", str(e), exc_info=True)  # exc_info=True 留堆栈，便于排查 SQLite 连接/查询异常根因
        return jsonify({
            "code": 500,
            "msg": f"获取账号列表失败: {str(e)}",
            "data": None
        }), 500


@app.route("/getValidAccounts",methods=['GET'])
async def getValidAccounts():
    """获取所有账号并验证cookie有效性，带异常保护和有效状态更新。
    
    为什么要加 try/except 包裹单账号检查：
    - 每个平台的 check_cookie 会启动独立浏览器进程，可能因环境问题抛异常
    - 某一个账号检查失败不能影响其他账号的结果返回，否则前端会看到 N-1 个状态
    - 异常的账号按"无效（0）"处理，并打印错误信息便于排障
    """
    with sqlite3.connect(str(DB_PATH)) as conn:
        cursor = conn.cursor()
        cursor.execute('''
        SELECT * FROM user_info''')
        rows = cursor.fetchall()
        rows_list = [list(row) for row in rows]
        # 调试信息：当前 user_info 原始行
        logger.debug("📋 当前数据表内容（检查前）: %s", rows_list)
        for i, row in enumerate(rows_list):
            try:
                flag = await check_cookie(row[1], row[2])
            except Exception as e:
                # 单账号检查异常：warning 级（因为 cookie 本身可能没问题，是环境/浏览器异常），并带堆栈方便定位是哪一个 check_cookie 抛错
                logger.warning("⚠️  检查账号 %s (type=%s) 时异常: %s", row[3], row[1], str(e), exc_info=True)
                flag = False
            if flag:
                # cookie 有效：更新状态为 1，并写库（前端才能显示"正常"）
                row[4] = 1
                cursor.execute('''
                UPDATE user_info 
                SET status = ? 
                WHERE id = ?
                ''', (1, row[0]))
                conn.commit()
                logger.info("✅ 账号 %s cookie 有效，状态已更新", row[3])
            else:
                # cookie 无效：更新状态为 0，并写库
                row[4] = 0
                cursor.execute('''
                UPDATE user_info 
                SET status = ? 
                WHERE id = ?
                ''', (0, row[0]))
                conn.commit()
                logger.warning("❌ 账号 %s cookie 无效，状态已更新", row[3])
        logger.debug("📋 验证后账号状态: %s", rows_list)
        return jsonify(
                        {
                            "code": 200,
                            "msg": None,
                            "data": rows_list
                        }),200

@app.route('/deleteFile', methods=['GET'])
def delete_file():
    file_id = request.args.get('id')

    if not file_id or not file_id.isdigit():
        return jsonify({
            "code": 400,
            "msg": "Invalid or missing file ID",
            "data": None
        }), 400

    try:
        # 获取数据库连接
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 查询要删除的记录
            cursor.execute("SELECT * FROM file_records WHERE id = ?", (file_id,))
            record = cursor.fetchone()

            if not record:
                return jsonify({
                    "code": 404,
                    "msg": "File not found",
                    "data": None
                }), 404

            record = dict(record)

            # 获取文件路径并删除实际文件
            file_path = Path(BASE_DIR / "videoFile" / record['file_path'])
            if file_path.exists():
                try:
                    file_path.unlink()  # 删除文件
                    logger.info("✅ 实际文件已删除: %s", file_path)
                except Exception as e:
                    logger.warning("⚠️ 删除实际文件失败: %s", e, exc_info=True)
                    # 即使删除文件失败，也要继续删除数据库记录，避免数据不一致
            else:
                logger.debug("⚠️ 实际文件不存在: %s", file_path)

            # 删除数据库记录
            cursor.execute("DELETE FROM file_records WHERE id = ?", (file_id,))
            conn.commit()

        return jsonify({
            "code": 200,
            "msg": "File deleted successfully",
            "data": {
                "id": record['id'],
                "filename": record['filename']
            }
        }), 200

    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str("delete failed!"),
            "data": None
        }), 500

@app.route('/deleteAccount', methods=['GET'])
def delete_account():
    account_id = request.args.get('id')

    if not account_id or not account_id.isdigit():
        return jsonify({
            "code": 400,
            "msg": "Invalid or missing account ID",
            "data": None
        }), 400

    account_id = int(account_id)

    try:
        # 获取数据库连接
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 查询要删除的记录
            cursor.execute("SELECT * FROM user_info WHERE id = ?", (account_id,))
            record = cursor.fetchone()

            if not record:
                return jsonify({
                    "code": 404,
                    "msg": "account not found",
                    "data": None
                }), 404

            record = dict(record)

            # 删除关联的cookie文件
            if record.get('filePath'):
                cookie_file_path = Path(BASE_DIR / "cookiesFile" / record['filePath'])
                if cookie_file_path.exists():
                    try:
                        cookie_file_path.unlink()
                        logger.info("✅ Cookie文件已删除: %s", cookie_file_path)
                    except Exception as e:
                        logger.warning("⚠️ 删除Cookie文件失败: %s", e, exc_info=True)

            # 删除数据库记录
            cursor.execute("DELETE FROM user_info WHERE id = ?", (account_id,))
            conn.commit()

        return jsonify({
            "code": 200,
            "msg": "account deleted successfully",
            "data": None
        }), 200

    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": f"delete failed: {str(e)}",
            "data": None
        }), 500


# SSE 登录接口
@app.route('/login')
def login():
    # 1 小红书 2 视频号 3 抖音 4 快手
    type = request.args.get('type')
    # 账号名
    id = request.args.get('id')

    # 模拟一个用于异步通信的队列
    status_queue = Queue()
    active_queues[id] = status_queue

    def on_close():
        logger.info("SSE 连接关闭，清理队列: %s", id)  # 加时间戳后能知道某个登录 SSE 流断开发生在几点，排查用户反馈"登录卡住没反应"时非常有用
        del active_queues[id]
    # 启动异步任务线程
    thread = threading.Thread(target=run_async_function, args=(type,id,status_queue), daemon=True)
    thread.start()
    response = Response(sse_stream(status_queue,), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'  # 关键：禁用 Nginx 缓冲
    response.headers['Content-Type'] = 'text/event-stream'
    response.headers['Connection'] = 'keep-alive'
    return response

@app.route('/postVideo', methods=['POST'])
def postVideo():
    # 获取JSON数据
    data = request.get_json()

    if not data:
        return jsonify({"code": 400, "msg": "请求数据不能为空", "data": None}), 400

    # 从JSON数据中提取fileList和accountList
    file_list = data.get('fileList', [])
    account_list = data.get('accountList', [])
    type = data.get('type')
    title = data.get('title')
    tags = data.get('tags')
    category = data.get('category')
    enableTimer = data.get('enableTimer')
    if category == 0:
        category = None
    productLink = data.get('productLink', '')
    productTitle = data.get('productTitle', '')
    thumbnail_path = data.get('thumbnail', '')
    is_draft = data.get('isDraft', False)  # 新增参数：是否保存为草稿

    videos_per_day = data.get('videosPerDay')
    daily_times = data.get('dailyTimes')
    start_days = data.get('startDays')

    # 参数校验
    if not file_list:
        return jsonify({"code": 400, "msg": "文件列表不能为空", "data": None}), 400
    if not account_list:
        return jsonify({"code": 400, "msg": "账号列表不能为空", "data": None}), 400
    if not type:
        return jsonify({"code": 400, "msg": "平台类型不能为空", "data": None}), 400
    if not title:
        return jsonify({"code": 400, "msg": "标题不能为空", "data": None}), 400

    # 为什么把 print 替换为 logger.debug：
    # file_list/account_list 属于请求参数调试信息，每次发布都会输出，量比较大，
    # 用 debug 级别避免 INFO 级别下刷屏；但一旦需要排障「用户到底传了什么账号/文件组合过来」，
    # 切换到 DEBUG 就能看到带时间戳的完整记录。
    logger.debug("File List: %s", file_list)
    logger.debug("Account List: %s", account_list)

    try:
        match type:
            case 1:
                post_video_xhs(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                                   start_days)
            case 2:
                post_video_tencent(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                                   start_days, is_draft)
            case 3:
                post_video_DouYin(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days, thumbnail_path, productLink, productTitle)
            case 4:
                post_video_ks(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days)
            case _:
                return jsonify({"code": 400, "msg": f"不支持的平台类型: {type}", "data": None}), 400

        # 返回响应给客户端
        return jsonify(
            {
                "code": 200,
                "msg": "发布任务已提交",
                "data": None
            }), 200
    except Exception as e:
        logger.error("发布视频时出错: %s", str(e), exc_info=True)  # 留堆栈：post_video_* 内部通常跑 Playwright，浏览器崩溃/超时最需要看堆栈
        return jsonify({
            "code": 500,
            "msg": f"发布失败: {str(e)}",
            "data": None
        }), 500


@app.route('/updateUserinfo', methods=['POST'])
def updateUserinfo():
    # 获取JSON数据
    data = request.get_json()

    # 从JSON数据中提取 type 和 userName
    user_id = data.get('id')
    type = data.get('type')
    userName = data.get('userName')
    try:
        # 获取数据库连接
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 更新数据库记录
            cursor.execute('''
                           UPDATE user_info
                           SET type     = ?,
                               userName = ?
                           WHERE id = ?;
                           ''', (type, userName, user_id))
            conn.commit()

        return jsonify({
            "code": 200,
            "msg": "account update successfully",
            "data": None
        }), 200

    except Exception as e:
        return jsonify({
            "code": 500,
            "msg": str("update failed!"),
            "data": None
        }), 500

@app.route('/postVideoBatch', methods=['POST'])
def postVideoBatch():
    data_list = request.get_json()

    if not isinstance(data_list, list):
        return jsonify({"code": 400, "msg": "Expected a JSON array", "data": None}), 400
    for data in data_list:
        # 从JSON数据中提取fileList和accountList
        file_list = data.get('fileList', [])
        account_list = data.get('accountList', [])
        type = data.get('type')
        title = data.get('title')
        tags = data.get('tags')
        category = data.get('category')
        enableTimer = data.get('enableTimer')
        if category == 0:
            category = None
        productLink = data.get('productLink', '')
        productTitle = data.get('productTitle', '')
        is_draft = data.get('isDraft', False)

        videos_per_day = data.get('videosPerDay')
        daily_times = data.get('dailyTimes')
        start_days = data.get('startDays')
        logger.debug("Batch - File List: %s", file_list)  # 批量发布场景下 file_list/account_list 输出较大，仍用 debug
        logger.debug("Batch - Account List: %s", account_list)
        match type:
            case 1:
                post_video_xhs(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                               start_days)
            case 2:
                post_video_tencent(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                                   start_days, is_draft)
            case 3:
                post_video_DouYin(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days, productLink, productTitle)
            case 4:
                post_video_ks(title, file_list, tags, account_list, category, enableTimer, videos_per_day, daily_times,
                          start_days)
    # 返回响应给客户端
    return jsonify(
        {
            "code": 200,
            "msg": None,
            "data": None
        }), 200

# Cookie文件上传API
@app.route('/uploadCookie', methods=['POST'])
def upload_cookie():
    try:
        if 'file' not in request.files:
            return jsonify({
                "code": 400,
                "msg": "没有找到Cookie文件",
                "data": None
            }), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({
                "code": 400,
                "msg": "Cookie文件名不能为空",
                "data": None
            }), 400

        if not file.filename.endswith('.json'):
            return jsonify({
                "code": 400,
                "msg": "Cookie文件必须是JSON格式",
                "data": None
            }), 400

        # 获取账号信息
        account_id = request.form.get('id')
        platform = request.form.get('platform')

        if not account_id or not platform:
            return jsonify({
                "code": 400,
                "msg": "缺少账号ID或平台信息",
                "data": None
            }), 400

        # 从数据库获取账号的文件路径
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('SELECT filePath FROM user_info WHERE id = ?', (account_id,))
            result = cursor.fetchone()

        if not result:
            return jsonify({
                "code": 500,
                "msg": "账号不存在",
                "data": None
            }), 404

        # 保存上传的Cookie文件到对应路径
        cookie_file_path = Path(BASE_DIR / "cookiesFile" / result['filePath'])
        cookie_file_path.parent.mkdir(parents=True, exist_ok=True)

        file.save(str(cookie_file_path))

        # 更新数据库中的账号信息（可选，比如更新更新时间）
        # 这里可以根据需要添加额外的处理逻辑

        return jsonify({
            "code": 200,
            "msg": "Cookie文件上传成功",
            "data": None
        }), 200

    except Exception as e:
        logger.error("上传Cookie文件时出错: %s", str(e), exc_info=True)  # 文件上传相关的异常（磁盘满/权限错/路径遍历攻击）必须留堆栈
        return jsonify({
            "code": 500,
            "msg": f"上传Cookie文件失败: {str(e)}",
            "data": None
        }), 500


# Cookie文件下载API
@app.route('/downloadCookie', methods=['GET'])
def download_cookie():
    try:
        file_path = request.args.get('filePath')
        if not file_path:
            return jsonify({
                "code": 500,
                "msg": "缺少文件路径参数",
                "data": None
            }), 400

        # 验证文件路径的安全性，防止路径遍历攻击
        cookie_file_path = Path(BASE_DIR / "cookiesFile" / file_path).resolve()
        base_path = Path(BASE_DIR / "cookiesFile").resolve()

        if not cookie_file_path.is_relative_to(base_path):
            return jsonify({
                "code": 500,
                "msg": "非法文件路径",
                "data": None
            }), 400

        if not cookie_file_path.exists():
            return jsonify({
                "code": 500,
                "msg": "Cookie文件不存在",
                "data": None
            }), 404

        # 返回文件
        return send_from_directory(
            directory=str(cookie_file_path.parent),
            path=cookie_file_path.name,
            as_attachment=True
        )

    except Exception as e:
        logger.error("下载Cookie文件时出错: %s", str(e), exc_info=True)  # 留堆栈：定位到底是文件不存在、权限、还是路径遍历防护被触发
        return jsonify({
            "code": 500,
            "msg": f"下载Cookie文件失败: {str(e)}",
            "data": None
        }), 500


# 包装函数：在线程中运行异步函数
def run_async_function(type,id,status_queue):
    match type:
        case '1':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(xiaohongshu_cookie_gen(id, status_queue))
            loop.close()
        case '2':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(get_tencent_cookie(id,status_queue))
            loop.close()
        case '3':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(douyin_cookie_gen(id,status_queue))
            loop.close()
        case '4':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(get_ks_cookie(id,status_queue))
            loop.close()

# SSE 流生成器函数
def sse_stream(status_queue):
    while True:
        if not status_queue.empty():
            msg = status_queue.get()
            yield f"data: {msg}\n\n"
        else:
            # 避免 CPU 占满
            time.sleep(0.1)

# ============================================================
# v2 API — Task tracking infrastructure
# ============================================================

# In-memory task store: {task_id: {task_id, platform_key, status, error, ...}}
task_store = {}
task_store_lock = threading.Lock()

# Cookies directory (new uploader system)
V2_COOKIES_DIR = Path(__file__).parent / "cookies"

# Platform key -> cookie-file prefix mapping
V2_PLATFORM_COOKIE_PREFIX = {
    "douyin": "douyin",
    "xiaohongshu": "xiaohongshu",
    "shipinhao": "tencent",
    "bilibili": "bilibili",
    "baijiahao": "baijiahao",
    "kuaishou": "kuaishou",
}


def create_task(platform_key, material_id=None, callback_url=None):
    task_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    task = {
        "task_id": task_id,
        "platform_key": platform_key,
        "status": "pending",
        "error": None,
        "publish_url": None,
        "material_id": material_id,
        "callback_url": callback_url,
        "created_at": now,
        "updated_at": now,
    }
    with task_store_lock:
        task_store[task_id] = task
    return task


def get_task(task_id):
    with task_store_lock:
        return task_store.get(task_id)


def update_task(task_id, **kwargs):
    with task_store_lock:
        task = task_store.get(task_id)
        if task:
            task.update(kwargs)
            task["updated_at"] = datetime.now().isoformat()
        return task


def send_callback(task):
    """POST result to callback_url with 3-attempt exponential backoff."""
    callback_url = task.get("callback_url")
    if not callback_url:
        return
    payload = {
        "task_id": task["task_id"],
        "platform_key": task["platform_key"],
        "status": task["status"],
        "error": task.get("error"),
        "publish_url": task.get("publish_url"),
    }
    for attempt in range(3):
        try:
            resp = http_requests.post(callback_url, json=payload, timeout=10)
            if resp.status_code < 300:
                return
        except Exception as e:
            # warning 级：偶尔 callback 失败（服务端短暂不可达）不致命，重试 3 次
            # 但必须留时间戳（重试间隔是指数退避 1s/2s/4s，排障时需要准确知道每次是几点重试的）
            logger.warning(
                "[v2] Callback attempt %d/3 failed task_id=%s url=%s error=%s",
                attempt + 1, task.get("task_id"), callback_url, str(e),
            )
        time.sleep(2 ** attempt)


def _resolve_account_file(platform_key, account_name="default"):
    """Return cookie file path for a given platform + account."""
    prefix = V2_PLATFORM_COOKIE_PREFIX.get(platform_key, platform_key)
    return V2_COOKIES_DIR / f"{prefix}_{account_name}.json"


def _find_default_account_name(platform_key):
    """Scan cookies dir for the first account of this platform, fallback to 'default'."""
    prefix = V2_PLATFORM_COOKIE_PREFIX.get(platform_key, platform_key)
    if V2_COOKIES_DIR.exists():
        for f in V2_COOKIES_DIR.iterdir():
            if f.name.startswith(f"{prefix}_") and f.name.endswith(".json"):
                # extract account_name from "{prefix}_{account_name}.json"
                stem = f.stem  # e.g. "douyin_myacc"
                return stem[len(prefix) + 1:]
    return "default"


# ---- lazy uploader imports (heavy deps like patchright) ----

def _import_uploader(platform_key):
    """Return a dict of callables for the given platform.
    Keys: setup, cookie_auth, VideoClass (may be None for bilibili).
    """
    if platform_key == "douyin":
        from uploader.douyin_uploader.main import (
            douyin_setup, cookie_auth, DouYinVideo,
        )
        return {"setup": douyin_setup, "cookie_auth": cookie_auth, "VideoClass": DouYinVideo}

    if platform_key == "xiaohongshu":
        from uploader.xiaohongshu_uploader.main import (
            xiaohongshu_setup, cookie_auth, XiaoHongShuVideo,
        )
        return {"setup": xiaohongshu_setup, "cookie_auth": cookie_auth, "VideoClass": XiaoHongShuVideo}

    if platform_key == "shipinhao":
        from uploader.tencent_uploader.main import (
            tencent_setup, cookie_auth, TencentVideo,
        )
        return {"setup": tencent_setup, "cookie_auth": cookie_auth, "VideoClass": TencentVideo}

    if platform_key == "kuaishou":
        from uploader.ks_uploader.main import (
            ks_setup, cookie_auth, KSVideo,
        )
        return {"setup": ks_setup, "cookie_auth": cookie_auth, "VideoClass": KSVideo}

    if platform_key == "baijiahao":
        from uploader.baijiahao_uploader.main import (
            baijiahao_setup, cookie_auth, BaiJiaHaoVideo,
        )
        return {"setup": baijiahao_setup, "cookie_auth": cookie_auth, "VideoClass": BaiJiaHaoVideo}

    if platform_key == "bilibili":
        # bilibili uses biliup CLI, no VideoClass
        return {"setup": None, "cookie_auth": None, "VideoClass": None}

    return None


async def _run_upload(platform_key, account_file, title, file_path, tags, description, publish_date):
    """Execute the actual upload for a given platform."""
    mods = _import_uploader(platform_key)
    if mods is None:
        raise ValueError(f"Unsupported platform: {platform_key}")

    # bilibili special path (CLI-based)
    if platform_key == "bilibili":
        from uploader.bilibili_uploader.runtime import run_biliup_command
        arguments = [
            "-u", str(account_file), "upload",
            str(file_path), "--title", title,
            "--desc", description or "", "--tid", "130",  # default tid
        ]
        if tags:
            arguments.extend(["--tag", ",".join(tags)])
        if publish_date and publish_date != 0:
            if isinstance(publish_date, datetime):
                arguments.extend(["--dtime", str(int(publish_date.timestamp()))])
        result = run_biliup_command(arguments)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "").strip() or "Bilibili upload failed")
        return

    # Common path: setup -> verify cookie
    setup_fn = mods["setup"]
    is_ready = await setup_fn(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Cookie missing or expired for {platform_key}: {account_file}. "
            f"Please login first via POST /api/v2/login/{platform_key}."
        )

    VideoClass = mods["VideoClass"]

    # Instantiate uploader per platform
    if platform_key == "douyin":
        from uploader.douyin_uploader.main import DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
        app_inst = VideoClass(
            title, str(file_path), tags or [], publish_date or 0,
            str(account_file), desc=description or "",
            publish_strategy=DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
            debug=True, headless=True,
        )
        await app_inst.douyin_upload_video()

    elif platform_key == "xiaohongshu":
        from uploader.xiaohongshu_uploader.main import XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
        app_inst = VideoClass(
            title=title, file_path=str(file_path), desc=description or "",
            tags=tags or [], publish_date=publish_date or 0,
            account_file=str(account_file),
            publish_strategy=XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
            debug=True, headless=True,
        )
        await app_inst.main()

    elif platform_key == "shipinhao":
        from uploader.tencent_uploader.main import TENCENT_PUBLISH_STRATEGY_IMMEDIATE
        app_inst = VideoClass(
            title=title, file_path=str(file_path), tags=tags or [],
            publish_date=publish_date or 0, account_file=str(account_file),
            desc=description or "",
            publish_strategy=TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
            debug=True, headless=True,
        )
        await app_inst.tencent_upload_video()

    elif platform_key == "kuaishou":
        from uploader.ks_uploader.main import KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE
        app_inst = VideoClass(
            title=title, file_path=str(file_path), desc=description or "",
            tags=tags or [], publish_date=publish_date or 0,
            account_file=str(account_file),
            publish_strategy=KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
            debug=True, headless=True,
        )
        await app_inst.main()

    elif platform_key == "baijiahao":
        app_inst = VideoClass(
            title=title, file_path=str(file_path), tags=tags or [],
            publish_date=publish_date or 0, account_file=str(account_file),
        )
        await app_inst.main()

    else:
        raise ValueError(f"Unsupported platform: {platform_key}")


def execute_publish_task(task, file_path, title, tags, description, scheduled_at, material_id=None, callback_url=None):
    """Runs in a background thread: executes upload and updates task."""
    try:
        update_task(task["task_id"], status="running", material_id=material_id, callback_url=callback_url)
        platform_key = task["platform_key"]
        account_name = _find_default_account_name(platform_key)
        account_file = _resolve_account_file(platform_key, account_name)

        # Parse scheduled_at
        publish_date = 0
        if scheduled_at:
            try:
                publish_date = datetime.fromisoformat(scheduled_at)
            except (ValueError, TypeError):
                publish_date = 0

        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                _run_upload(platform_key, account_file, title, file_path, tags, description, publish_date)
            )
        finally:
            loop.close()

        update_task(task["task_id"], status="success")
        logger.info(
            "[v2] Task %s (%s) succeeded. material_id=%s",
            task.get("task_id"), platform_key, material_id,
        )
    except Exception as e:
        logger.error(
            "[v2] Task %s (%s) failed: %s",
            task.get("task_id"), task.get("platform_key"), str(e),
            exc_info=True,  # 发布失败通常带浏览器异常，必须留堆栈
        )
        update_task(task["task_id"], status="failed", error=str(e))
    finally:
        # Reload task for callback
        current = get_task(task["task_id"])
        if current:
            send_callback(current)


def execute_login_task(task, platform_key, account_name):
    """Runs in a background thread: executes login flow."""
    try:
        update_task(task["task_id"], status="running")
        account_file = _resolve_account_file(platform_key, account_name)
        account_file.parent.mkdir(parents=True, exist_ok=True)

        if platform_key == "bilibili":
            # bilibili login requires interactive terminal, just mark as needing manual action
            update_task(task["task_id"], status="failed",
                        error="Bilibili login requires interactive terminal. "
                              "Run `sau bilibili login --account {}` manually.".format(account_name))
            return

        mods = _import_uploader(platform_key)
        if mods is None or mods.get("setup") is None:
            update_task(task["task_id"], status="failed", error=f"Unsupported platform: {platform_key}")
            return

        setup_fn = mods["setup"]
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                setup_fn(str(account_file), handle=True, return_detail=True, headless=True)
            )
        finally:
            loop.close()

        if isinstance(result, dict) and result.get("success"):
            update_task(task["task_id"], status="success")
        elif isinstance(result, dict) and not result.get("success"):
            update_task(task["task_id"], status="failed", error=result.get("message", "Login failed"))
        else:
            # setup returned True/non-dict — treat as success
            update_task(task["task_id"], status="success")

        # 日志里明确打印 setup 返回的 success / message，定位「登录没拿到 cookie」问题
        logger.info(
            "[v2] Login task %s (%s) completed. account=%s result_type=%s success=%s message=%s",
            task.get("task_id"), platform_key, account_name,
            type(result).__name__,
            (result.get("success") if isinstance(result, dict) else None),
            (result.get("message") if isinstance(result, dict) else "N/A"),
        )
    except Exception as e:
        logger.error(
            "[v2] Login task %s (%s) failed: %s",
            task.get("task_id"), platform_key, str(e),
            exc_info=True,  # 登录流程经常是 Playwright 异常，必须留堆栈
        )
        update_task(task["task_id"], status="failed", error=str(e))


# ============================================================
# v2 API Endpoints
# ============================================================

@app.route('/api/v2/accounts', methods=['GET'])
def v2_accounts():
    """List all accounts by scanning cookies/ directory."""
    accounts = []
    if not V2_COOKIES_DIR.exists():
        return jsonify({"code": 200, "msg": None, "data": {"accounts": []}}), 200

    # Reverse map: cookie prefix -> platform_key
    prefix_to_platform = {}
    for pk, prefix in V2_PLATFORM_COOKIE_PREFIX.items():
        prefix_to_platform.setdefault(prefix, []).append(pk)

    for f in V2_COOKIES_DIR.iterdir():
        if not f.name.endswith('..json') and f.suffix == '.json':
            # Parse filename: {prefix}_{account_name}.json
            stem = f.stem  # e.g. "douyin_default"
            parts = stem.split('_', 1)
            if len(parts) != 2:
                continue
            prefix, account_name = parts
            platforms = prefix_to_platform.get(prefix, [prefix])
            stat = f.stat()
            for pk in platforms:
                accounts.append({
                    "platform_key": pk,
                    "account_name": account_name,
                    "cookie_file": str(f),
                    "is_valid": None,  # not checked (too slow)
                    "last_modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })

    return jsonify({"code": 200, "msg": None, "data": {"accounts": accounts}}), 200


@app.route('/api/v2/login/<platform_key>', methods=['POST'])
def v2_login(platform_key):
    """Start a login flow for the given platform."""
    valid_platforms = list(V2_PLATFORM_COOKIE_PREFIX.keys())
    if platform_key not in valid_platforms:
        return jsonify({"code": 400, "msg": f"不支持的平台: {platform_key}，可选: {valid_platforms}"}), 400

    data = request.get_json(silent=True) or {}
    account_name = data.get('account_name', 'default')

    task = create_task(platform_key)
    t = threading.Thread(
        target=execute_login_task,
        args=(task, platform_key, account_name),
        daemon=True,
    )
    t.start()

    return jsonify({"code": 200, "msg": "登录流程已启动", "data": {"task_id": task["task_id"], "status": "login_started"}}), 200


@app.route('/api/v2/callback', methods=['POST'])
def v2_callback():
    """Receive task result callback from opcgeo server or external agent."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"code": 400, "msg": "请求数据不能为空"}), 400

    task_id = data.get('task_id')
    status = data.get('status')
    if not task_id or not status:
        return jsonify({"code": 400, "msg": "缺少 task_id 或 status"}), 400

    task = get_task(task_id)
    if not task:
        return jsonify({"code": 404, "msg": "任务不存在"}), 404

    update_fields = {"status": status}
    if data.get('error'):
        update_fields["error"] = data["error"]
    if data.get('publish_url'):
        update_fields["publish_url"] = data["publish_url"]

    update_task(task_id, **update_fields)
    return jsonify({"code": 200, "msg": "回调已接收", "data": {"task_id": task_id}}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0' ,port=5409)
