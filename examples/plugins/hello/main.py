from telegram_task_runner.plugin_api import result


def handle(request, ctx):
    return result('你好，' + (request['argument'] or '朋友') + '！当前插件任务：' + ctx.job_id)
