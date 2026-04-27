package com.marketradar.app.util

import com.chaquo.python.Python

object LogTap {
    fun install(app: android.app.Application) {
        // Crash handler
        val prior = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { t, e ->
            LogBuffer.add('F', "UncaughtException",
                "thread=${t.name} ${e::class.java.simpleName}: ${e.message}\n${e.stackTraceToString()}")
            prior?.uncaughtException(t, e)
        }
    }

    fun installPythonStreams() {
        try {
            val py = Python.getInstance()
            val sys = py.getModule("sys")
            val builtins = py.getModule("builtins")
            py.getModule("__main__").callAttr("exec", """
import sys
class _LogTapStream:
    def __init__(self, level):
        self._level = level
        self._buf = ''
    def write(self, s):
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            if line:
                _logtap_emit(self._level, line)
    def flush(self): pass
sys.stdout = _LogTapStream('stdout')
sys.stderr = _LogTapStream('stderr')
            """.trimIndent())
            builtins.put("_logtap_emit", PyLogEmitter())
        } catch (e: Exception) {
            LogBuffer.add('E', "LogTap", "Python redirect failed: ${e.message}")
        }
    }

    private class PyLogEmitter {
        @Suppress("unused")
        fun __call__(level: String, line: String) {
            val tag = if (level == "stderr") "py.stderr" else "py.stdout"
            val severity = if (level == "stderr") 'W' else 'I'
            LogBuffer.add(severity, tag, line)
        }
    }
}
