/*
 * LGTBot_ElainaBot.cc — LGTBot × ElainaBot (QQ Official Bot) 桥接层
 *
 * 将 LGTBot C++ 引擎通过 Boost.Python 暴露给 Python，
 * Python 侧由 ElainaBot 插件系统提供消息收发能力。
 *
 * 与 lgtbot_kook.cc 的主要差异：
 *   1. Boost.Python 模块名改为 LGTBot_ElainaBot
 *   2. 用户 Mention 格式：Kook (met)uid(met) → QQ Markdown <@uid>
 */

// 抑制 Boost 自身链路里残留的 deprecation `#pragma message`：
//   · BOOST_BIND_GLOBAL_PLACEHOLDERS —— 显式接受 _1 / _2 仍在全局命名空间
//   · BOOST_ALLOW_DEPRECATED_HEADERS —— 接受 boost.python 仍然间接引用
//                                       已被 Boost 标记为 deprecated 的内部头
// 两个都是 Boost 官方给消费者的 opt-in 开关，只关警告，不改行为。
#define BOOST_BIND_GLOBAL_PLACEHOLDERS
#define BOOST_ALLOW_DEPRECATED_HEADERS

#include <boost/python.hpp>
#include <boost/python/call.hpp>

#include "bot_core/bot_core.h"

#include <memory>
#include <thread>
#include <iostream>
#include <curl/curl.h>

#include <csignal>
#include <csetjmp>
#include <cstring>
#include <ctime>
#include <exception>       // std::set_terminate
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>      // mkdir
#include <sys/syscall.h>   // SYS_gettid
#include <execinfo.h>      // backtrace, backtrace_symbols_fd

// ──── 全局 Python 回调句柄 ────────────────────────────────────────────────
void* g_bot_core              = nullptr;
PyObject* g_get_user_name     = nullptr;
PyObject* g_get_user_avatar_url = nullptr;
PyObject* g_send_text_message = nullptr;
PyObject* g_send_image_message = nullptr;
PyObject* g_match_event       = nullptr;

// ──── GIL 辅助 RAII ──────────────────────────────────────────────────────
class AcquireGIL {
public:
    inline AcquireGIL()  { state = PyGILState_Ensure(); }
    inline ~AcquireGIL() { PyGILState_Release(state);   }
private:
    PyGILState_STATE state;
};

class ReleaseGIL {
public:
    inline ReleaseGIL()  { save_state = PyEval_SaveThread();  }
    inline ~ReleaseGIL() { PyEval_RestoreThread(save_state);  }
private:
    PyThreadState* save_state;
};

// ──── SIGSEGV / SIGBUS 防护:不让 lgtbot 段错误带垮 Python 主框架 ──────────
//
// 设计:
//   1. `OnPrivate/PublicMessage` 进入时把上下文(uid/gid/msg)写到 thread_local
//      char 缓冲,然后 ``sigsetjmp`` 设回退点,再调 lgtbot。
//   2. lgtbot 内部触发 SIGSEGV/SIGBUS → `SigSegvHandler` 只做 async-signal-safe
//      操作(``write(2)`` 写一行 stderr + ``siglongjmp`` 跳出),其余收尾交给
//      Python 侧。
//   3. 回到 wrapper 后,GIL 状态不确定(`ReleaseGIL` 的 dtor 被 longjmp 跳过没跑,
//      GIL 仍处于释放态),用 `PyGILState_Ensure` 重新拿;然后调 Python 模块
//      ``plugins.LGTBot_ElainaBot.mod.callbacks.cb_lgtbot_crashed`` 通知崩溃,
//      Python 侧负责 WebUI 日志 / 发道歉 / 30s 后 os.execv 重启。
//   4. `thread_local sigjmp_buf` 让多线程并发的引擎调用各自独立恢复 —— 信号
//      处理器在出错线程上运行,读到的 TLS 就是该线程的,不会互相干扰。
//   5. SIGSEGV 后 lgtbot 内部状态损坏,Python 侧拿到通知后立刻把 state.started
//      置 False,避免后续派发去戳已经废了的引擎触发二次崩溃;30s 后 execv
//      整进程重启,彻底重建。

namespace {

thread_local sigjmp_buf t_sigsegv_jmpbuf;
thread_local volatile sig_atomic_t t_sigsegv_armed = 0;

// 崩溃上下文:wrapper 在 sigsetjmp 之前填,longjmp 恢复后透传给 Python
thread_local char t_crash_uid[128];
thread_local char t_crash_gid[128];
thread_local char t_crash_msg[512];
thread_local volatile sig_atomic_t t_crash_is_uid = 0;

// 崩溃栈 dump 文件夹绝对路径(`<plugin_dir>/LGTBot_CRASH_DUMPS`)。
// 在 InstallSigSegvHandler 里由 game_path 推导一次,handler 只读。
// 留 1024 字节足够装绝对路径 + 后缀。空字符串 = 推导失败,dump 跳过。
char g_crash_dump_dir[1024] = {0};

// ──────── SIGABRT / terminate execv 自启所需的全局状态 ──────────────────────
// SetRestartArgs 把 sys.executable + sys.argv 固化进这些静态 buffer,SigAbrtHandler
// / OnCxxTerminate 在 heap 已坏时无需任何分配即可直接 execv。
static constexpr size_t kExecPathMax = 4096;
static constexpr size_t kExecArgvBufMax = 16384;
static constexpr int    kExecArgvMax = 64;
char g_exec_path[kExecPathMax] = {0};
char g_exec_argv_buf[kExecArgvBufMax] = {0};
char* g_exec_argv[kExecArgvMax + 1] = {nullptr};
volatile sig_atomic_t g_exec_argv_ready = 0;
volatile sig_atomic_t g_already_aborting = 0;

// ──────── async-signal-safe 串行写工具 ──────────────────────────────────────
// 不用 printf/snprintf/fprintf —— 这些理论上可能调 locale 数据、可能死锁。
// 全部手写,逻辑极简,只用 write(2) 这一个 syscall。
inline void as_write_str(int fd, const char* s) {
    if (!s) return;
    size_t n = std::strlen(s);
    ssize_t r = write(fd, s, n);
    (void)r;
}
inline void as_write_n(int fd, const char* s, size_t n) {
    ssize_t r = write(fd, s, n);
    (void)r;
}
// 把 unsigned long 转成十进制字符串(末端对齐),返回首字符指针。
// buf 至少 24 字节(2^64 最多 20 位 + 终止符 + 余量)。
inline char* as_uint_to_dec(unsigned long v, char* end) {
    *--end = '\0';
    if (v == 0) {
        *--end = '0';
    } else {
        while (v) {
            *--end = static_cast<char>('0' + (v % 10));
            v /= 10;
        }
    }
    return end;
}
inline char* as_uint_to_hex(unsigned long v, char* end) {
    static const char hex[] = "0123456789abcdef";
    *--end = '\0';
    if (v == 0) {
        *--end = '0';
    } else {
        while (v) {
            *--end = hex[v & 0xf];
            v >>= 4;
        }
    }
    return end;
}
inline void as_write_uint(int fd, unsigned long v) {
    char buf[24];
    as_write_str(fd, as_uint_to_dec(v, buf + sizeof(buf)));
}
inline void as_write_hex(int fd, unsigned long v) {
    as_write_str(fd, "0x");
    char buf[24];
    as_write_str(fd, as_uint_to_hex(v, buf + sizeof(buf)));
}

// 把 dump 文件路径拼到 out:<dir>/crash_<sec>_<pid>_<tid>.log
// 返回拼出的总长度(不含终止符);overflow 返回 0(handler 应丢弃 dump)。
inline size_t as_build_dump_path(char* out, size_t cap,
                                 const char* dir,
                                 long sec, int pid, int tid)
{
    size_t len = 0;
    auto try_append = [&](const char* s) -> bool {
        size_t n = std::strlen(s);
        if (len + n + 1 > cap) return false;
        std::memcpy(out + len, s, n);
        len += n;
        out[len] = '\0';
        return true;
    };
    char numbuf[24];
    if (!try_append(dir)) return 0;
    if (!try_append("/crash_")) return 0;
    if (!try_append(as_uint_to_dec((unsigned long)sec, numbuf + sizeof(numbuf)))) return 0;
    if (!try_append("_")) return 0;
    if (!try_append(as_uint_to_dec((unsigned long)pid, numbuf + sizeof(numbuf)))) return 0;
    if (!try_append("_")) return 0;
    if (!try_append(as_uint_to_dec((unsigned long)tid, numbuf + sizeof(numbuf)))) return 0;
    if (!try_append(".log")) return 0;
    return len;
}

// 把崩溃信息 dump 到 g_crash_dump_dir/crash_<sec>_<pid>_<tid>.log
// 所有调用必须是 async-signal-safe:open/write/close/mkdir/clock_gettime/
// getpid/syscall(SYS_gettid)/backtrace/backtrace_symbols_fd。无 malloc。
inline void DumpCrashToFile(int sig, siginfo_t* info) {
    if (g_crash_dump_dir[0] == '\0') return;

    // mkdir 在 Install 已经做过,但每次 dump 再 mkdir 一次防止有人手贱删了
    // 文件夹;EEXIST 算成功,handler 不关心返回值。
    (void)mkdir(g_crash_dump_dir, 0755);

    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    pid_t pid = getpid();
    pid_t tid = static_cast<pid_t>(syscall(SYS_gettid));

    char path[1024];
    size_t plen = as_build_dump_path(path, sizeof(path),
                                     g_crash_dump_dir,
                                     (long)ts.tv_sec, (int)pid, (int)tid);
    if (plen == 0) return;

    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;

    as_write_str(fd, "=== LGTBot crash captured ===\n");
    as_write_str(fd, "time_sec: ");  as_write_uint(fd, (unsigned long)ts.tv_sec);
    as_write_str(fd, "\ntime_nsec: "); as_write_uint(fd, (unsigned long)ts.tv_nsec);
    as_write_str(fd, "\nsignal: ");  as_write_uint(fd, (unsigned long)sig);
    if (info) {
        as_write_str(fd, "\nsi_addr: ");
        as_write_hex(fd, reinterpret_cast<unsigned long>(info->si_addr));
        as_write_str(fd, "\nsi_code: ");
        as_write_uint(fd, (unsigned long)info->si_code);
    }
    as_write_str(fd, "\npid: ");  as_write_uint(fd, (unsigned long)pid);
    as_write_str(fd, "\ntid: ");  as_write_uint(fd, (unsigned long)tid);
    as_write_str(fd, "\nis_uid: "); as_write_uint(fd, (unsigned long)t_crash_is_uid);
    as_write_str(fd, "\nuid: ");  as_write_str(fd, t_crash_uid);
    as_write_str(fd, "\ngid: ");  as_write_str(fd, t_crash_gid);
    as_write_str(fd, "\nmsg: ");  as_write_str(fd, t_crash_msg);
    as_write_str(fd, "\n\n--- backtrace ---\n");

    void* frames[64];
    int frame_count = backtrace(frames, 64);
    // backtrace_symbols_fd 不分配内存,直接 write(fd) —— 是为 signal handler
    // 准备的,glibc 文档明示。
    backtrace_symbols_fd(frames, frame_count, fd);
    as_write_str(fd, "\n=== end ===\n");

    close(fd);
}

// 信号处理器 —— 只能用 async-signal-safe 函数(POSIX 列出的那一小撮):
// `write`、`raise`、`signal`、`open`、`close`、`mkdir`、`clock_gettime`、
// `backtrace*` 这类。**不能**用 printf / malloc / boost / Python C API。
void SigSegvHandler(int sig, siginfo_t* info, void* /*ucontext*/)
{
    if (t_sigsegv_armed) {
        t_sigsegv_armed = 0;
        static const char banner[] =
            "\n[LGTBot] FATAL: lgtbot SIGSEGV/SIGBUS captured, dumping stack and recovering\n";
        ssize_t r = write(STDERR_FILENO, banner, sizeof(banner) - 1);
        (void)r;

        // 在 longjmp 之前把栈和上下文落盘 —— 即便后续 backtrace 自己再次出错
        // (nested SEGV) 也会被 handler 二次进入时 t_sigsegv_armed==0 分支
        // 走 SIG_DFL 终结,跟改造前等价,不比 baseline 更差。
        DumpCrashToFile(sig, info);

        siglongjmp(t_sigsegv_jmpbuf, sig);
    }
    // 不在保护区(进程启动 / 其他 .so 出问题等):降回默认动作,杀进程
    std::signal(sig, SIG_DFL);
    raise(sig);
}

// 从 game_path 推导 plugin 目录,再拼出 crash dump 目录。
// game_path 形如 ".../plugins/LGTBot_ElainaBot/build/plugins" —— 去掉
// "/build/plugins" 后缀就是 plugin 根目录。失败时 g_crash_dump_dir 保持空,
// handler 里的 dump 会自动跳过。
//
// 预编译模式 game_path 形如 ".../LGTBot_ElainaBot/build_prebuilt/build/plugins",
// 上面截出的 base 末尾会多一层 "/build_prebuilt";剥掉它,让崩溃转储无论本地 /
// 预编译都**固定落在插件根** /LGTBot_CRASH_DUMPS,面板才统一读得到。
inline void DeriveCrashDumpDir(const char* game_path) {
    if (!game_path) return;
    const char marker[] = "/build/plugins";
    const char* found = std::strstr(game_path, marker);
    if (!found) return;
    size_t base_len = static_cast<size_t>(found - game_path);
    const char pre[] = "/build_prebuilt";
    const size_t pre_len = sizeof(pre) - 1;   // 不含 '\0'
    if (base_len >= pre_len &&
        std::memcmp(game_path + base_len - pre_len, pre, pre_len) == 0) {
        base_len -= pre_len;                  // 预编译:再剥掉 /build_prebuilt → 插件根
    }
    const char suffix[] = "/LGTBot_CRASH_DUMPS";
    if (base_len + sizeof(suffix) > sizeof(g_crash_dump_dir)) return;
    std::memcpy(g_crash_dump_dir, game_path, base_len);
    std::memcpy(g_crash_dump_dir + base_len, suffix, sizeof(suffix));  // 含 '\0'
}

// ──────── 二次崩溃兜底:SIGABRT 拦截 + 预存 execv 参数 ───────────────────────
// 任何 SIGABRT 都意味着进程必死(glibc abort 的「双杀」逻辑:即便 handler 接住
// 第一次 raise,abort 也会把 handler 重置成 SIG_DFL 再 raise 一次)。所以一律
// execv 整进程自启 —— 永远比陪葬强。已知三种 abort 来源:
//   1. SEGV 后 heap 损坏,工作线程退出时 tcache_thread_shutdown double-free;
//   2. **静默 heap 腐败(无前置 SEGV)** 同样在 tcache_thread_shutdown 暴露;
//   3. 其它 assert / abort()。
//
// 关键前置(Start 时 Python 调 set_restart_args):把 sys.executable + sys.argv
// 固化进静态 buffer,handler 在 heap 已坏时无需任何分配即可 execv。
//
// 死循环熔断:若 heap 腐败是确定性的,会每次重启又立刻 abort → 紧凑 execv 死
// 循环烧 CPU。handler 经 WriteApologyMarker 往 abort_restart_history 追加一行
// 时间戳,Python 启动时 callbacks.check_crash_loop() 读历史,窗口内超阈值就暂停
// 启动引擎(主框架保持运行)并告警,人工修复后热重载即复位。
//
// 全局状态 (g_exec_path / g_exec_argv / g_already_aborting) 在文件靠前已声明。

inline void WriteApologyMarker(const char* sig_kind) noexcept;  // 定义见下方

void SigAbrtHandler(int sig, siginfo_t* info, void* /*ucontext*/) {
    // 防 handler 自己再 abort 进死循环(仅 execv 失败 fallback 路径可能触发)
    if (g_already_aborting) {
        std::signal(sig, SIG_DFL);
        raise(sig);
        return;
    }
    g_already_aborting = 1;

    static const char banner[] =
        "\n[LGTBot] SIGABRT trapped, forcing execv self-restart\n";
    ssize_t r = write(STDERR_FILENO, banner, sizeof(banner) - 1);
    (void)r;

    // 先落 marker + 重启时间戳(便宜、低故障风险),再尝试 backtrace dump
    // —— 放弃 core 换 uptime;backtrace 万一在腐败 heap 上二次出错,补发 marker
    // 与熔断计数也已先保住。两者都是 async-signal-safe。
    WriteApologyMarker("sigabrt");
    DumpCrashToFile(sig, info);

    // 重置三个 handler 为默认,防 execv 失败回落 abort 链路时再被自己卷入双杀
    std::signal(SIGABRT, SIG_DFL);
    std::signal(SIGSEGV, SIG_DFL);
    std::signal(SIGBUS,  SIG_DFL);

    if (g_exec_argv_ready && g_exec_path[0] && g_exec_argv[0]) {
        execv(g_exec_path, g_exec_argv);
        // execv 失败(仅 sys.executable 失踪等罕见场景才到这)
        static const char fail[] = "[LGTBot] execv failed in SIGABRT handler\n";
        r = write(STDERR_FILENO, fail, sizeof(fail) - 1);
        (void)r;
    } else {
        static const char noargs[] = "[LGTBot] no execv args stashed, dying\n";
        r = write(STDERR_FILENO, noargs, sizeof(noargs) - 1);
        (void)r;
    }
    // 走到这就是 execv 也失败了,默认 abort 让 supervisor 兜底
    std::signal(sig, SIG_DFL);
    raise(sig);
}

// ──────── execv 前夜:写「待补发道歉」marker 文件 ─────────────────────────
// OnCxxTerminate 不敢碰 Python(异常上下文 + heap 边缘);但用户体验上,玩家
// 仍然该收到一句"游戏崩了"的道歉,管理员群也该收到崩溃通知。折衷:崩溃的
// 这一刻只用 async-signal-safe syscall 把"该发什么、发给谁"写到一个 marker
// 文件,execv 重启后由干净进程的 Python 侧扫这个目录,读出来异步补发。
//
// 文件:`<g_crash_dump_dir>/pending_apology_<sec>_<pid>_<tid>.txt`
// 格式 (Python 侧 callbacks.py::_parse_apology_marker 配套解析):
//
//   sig=cxx_terminate
//   is_uid=0
//   ts=1780439222
//   uid_len=10
//   uid=<10 字节原文>
//   gid_len=8
//   gid=<8 字节原文>
//   msg_len=12
//   msg=<12 字节原文,可含 \n / 任意字节>
//
// uid/gid/msg 走 length-prefix 是因为 user message 可能含换行 / 二进制字节,
// 不想在 async-signal-safe 上下文里写 escape 代码。
//
// 所有调用都是 async-signal-safe:mkdir/clock_gettime/getpid/syscall/open/
// write/close,以及上面已有的 as_write_* 手写工具(只调 write(2))。
inline void WriteApologyMarker(const char* sig_kind) noexcept {
    if (g_crash_dump_dir[0] == '\0') return;
    (void)mkdir(g_crash_dump_dir, 0755);

    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    pid_t pid = getpid();
    pid_t tid = static_cast<pid_t>(syscall(SYS_gettid));

    // 拼路径:<dir>/pending_apology_<sec>_<pid>_<tid>.txt
    char path[1024];
    size_t len = 0;
    auto try_append = [&](const char* s) -> bool {
        size_t n = std::strlen(s);
        if (len + n + 1 > sizeof(path)) return false;
        std::memcpy(path + len, s, n);
        len += n;
        path[len] = '\0';
        return true;
    };
    char numbuf[24];
    if (!try_append(g_crash_dump_dir)) return;
    if (!try_append("/pending_apology_")) return;
    if (!try_append(as_uint_to_dec((unsigned long)ts.tv_sec, numbuf + sizeof(numbuf)))) return;
    if (!try_append("_")) return;
    if (!try_append(as_uint_to_dec((unsigned long)pid, numbuf + sizeof(numbuf)))) return;
    if (!try_append("_")) return;
    if (!try_append(as_uint_to_dec((unsigned long)tid, numbuf + sizeof(numbuf)))) return;
    if (!try_append(".txt")) return;

    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (fd < 0) return;

    as_write_str(fd, "sig=");
    as_write_str(fd, sig_kind);
    as_write_str(fd, "\n");
    as_write_str(fd, "is_uid=");
    as_write_uint(fd, (unsigned long)t_crash_is_uid);
    as_write_str(fd, "\nts=");
    as_write_uint(fd, (unsigned long)ts.tv_sec);

    size_t ul = std::strlen(t_crash_uid);
    as_write_str(fd, "\nuid_len=");
    as_write_uint(fd, (unsigned long)ul);
    as_write_str(fd, "\nuid=");
    as_write_n(fd, t_crash_uid, ul);

    size_t gl = std::strlen(t_crash_gid);
    as_write_str(fd, "\ngid_len=");
    as_write_uint(fd, (unsigned long)gl);
    as_write_str(fd, "\ngid=");
    as_write_n(fd, t_crash_gid, gl);

    size_t ml = std::strlen(t_crash_msg);
    as_write_str(fd, "\nmsg_len=");
    as_write_uint(fd, (unsigned long)ml);
    as_write_str(fd, "\nmsg=");
    as_write_n(fd, t_crash_msg, ml);

    as_write_str(fd, "\n");
    close(fd);

    // 追加一行重启时间戳到 abort_restart_history,供 Python 启动时
    // check_crash_loop() 做崩溃死循环熔断。复用 ts(本次崩溃时刻)。
    char hist[1056];
    size_t hl = 0;
    auto hist_append = [&](const char* s) -> bool {
        size_t n = std::strlen(s);
        if (hl + n + 1 > sizeof(hist)) return false;
        std::memcpy(hist + hl, s, n);
        hl += n;
        hist[hl] = '\0';
        return true;
    };
    if (hist_append(g_crash_dump_dir) && hist_append("/abort_restart_history")) {
        int hfd = open(hist, O_WRONLY | O_CREAT | O_APPEND, 0644);
        if (hfd >= 0) {
            char nb[24];
            as_write_str(hfd, as_uint_to_dec((unsigned long)ts.tv_sec, nb + sizeof(nb)));
            as_write_str(hfd, "\n");
            close(hfd);
        }
    }
}

// ──────── 第三道防线:std::set_terminate handler ──────────────────────────
// 处理 SIGSEGV/SigAbrtHandler 兜不住的崩溃路径:C++ 异常 unwind 找不到 catch
// → c++ runtime 调 std::terminate()。已知触发点:lgtbot 上游
// match_child_client.cc::WaitForResponse_ 从 pipe 反序列化 protobuf 时,脏数据
// 被解释成 16 亿元素长度,RepeatedPtrFieldBase::InternalExtend 抛 std::bad_alloc
//
// 默认 terminate 行为是调 abort(),走 glibc「双杀」逻辑:即便 SigAbrtHandler
// 接住了第一次 raise(SIGABRT),abort() 也会强制把 handler 重置回 SIG_DFL 再
// raise 一次,确保进程死亡。所以唯一可靠的接管点就是这个 terminate_handler ——
// 它在异常 unwind 失败时由 c++ runtime 直接调用,**早于** abort()/SIGABRT 链路
// 启动,有机会跳过 abort() 直接 execv 自启。
//
// 与 SigAbrtHandler(只在 post-SEGV 状态下接管)互补:本 handler 覆盖所有未捕获
// 的 C++ 异常路径,无论之前有没有 SEGV。
//
// 必须永不返回(返回会让 c++ runtime 兜底调 abort())。只用 async-signal-safe
// 路径:write/execv/_exit,绝不碰 Python C API(异常上下文 GIL 状态不可知)
// 或 malloc(heap 可能已坏)。
[[noreturn]] void OnCxxTerminate() noexcept {
    // 防递归:execv 失败后哪怕只是个偶然又触发 terminate,第二次直接 _exit
    // 让 supervisor 兜底,避免无限循环占用 CPU。
    static volatile sig_atomic_t s_in_terminate = 0;
    if (s_in_terminate) {
        static const char nested[] = "[LGTBot] nested terminate, _exit\n";
        ssize_t r = write(STDERR_FILENO, nested, sizeof(nested) - 1);
        (void)r;
        _exit(134);
    }
    s_in_terminate = 1;

    static const char banner[] =
        "\n[LGTBot] std::terminate trapped (uncaught C++ exception), forcing execv self-restart\n";
    ssize_t r = write(STDERR_FILENO, banner, sizeof(banner) - 1);
    (void)r;

    // 把崩溃上下文写到 marker 文件,execv 后干净进程会扫到并补发道歉 + 通知。
    WriteApologyMarker("cxx_terminate");

    // 把 SIGABRT/SIGSEGV/SIGBUS handler 全部重置成 SIG_DFL,以防 execv 失败
    // 落回 abort 链路时又被我们自己的 handler 卷进双杀流程。
    std::signal(SIGABRT, SIG_DFL);
    std::signal(SIGSEGV, SIG_DFL);
    std::signal(SIGBUS,  SIG_DFL);

    if (g_exec_argv_ready && g_exec_path[0] && g_exec_argv[0]) {
        execv(g_exec_path, g_exec_argv);
        // execv 失败极罕见(sys.executable 失踪 / 文件系统只读等)
        static const char fail[] = "[LGTBot] execv failed in terminate handler\n";
        r = write(STDERR_FILENO, fail, sizeof(fail) - 1);
        (void)r;
    } else {
        static const char noargs[] = "[LGTBot] no execv args stashed in terminate handler\n";
        r = write(STDERR_FILENO, noargs, sizeof(noargs) - 1);
        (void)r;
    }
    // 128 + SIGABRT(6) = 134,供 systemd / supervisor 识别异常退出语义
    _exit(134);
}

// Python 启动时把 sys.executable + sys.argv 喂进来,固化到静态 buffer
// 供 SigAbrtHandler / OnCxxTerminate 在 heap 已坏 / 异常上下文里使用。
void SetRestartArgs(const std::string& exec_path, boost::python::list argv) {
    // ── 主程序路径 ──────────────────────────────────────────────
    size_t exec_len = exec_path.size();
    if (exec_len >= sizeof(g_exec_path)) exec_len = sizeof(g_exec_path) - 1;
    std::memcpy(g_exec_path, exec_path.data(), exec_len);
    g_exec_path[exec_len] = '\0';

    // ── argv 数组:第 0 项与 exec_path 同义,后接 Python 传来的 sys.argv ──
    // 全部塞进同一个 buffer,各串以 '\0' 分隔;g_exec_argv[i] 指到对应起点。
    char* p = g_exec_argv_buf;
    char* end = g_exec_argv_buf + sizeof(g_exec_argv_buf);
    int argc = 0;

    auto append = [&](const char* s, size_t n) -> bool {
        if (argc >= kExecArgvMax) return false;
        if (p + n + 1 > end) return false;
        g_exec_argv[argc] = p;
        std::memcpy(p, s, n);
        p[n] = '\0';
        p += n + 1;
        ++argc;
        return true;
    };

    append(exec_path.data(), exec_len);  // argv[0]
    const int n = static_cast<int>(boost::python::len(argv));
    for (int i = 0; i < n; ++i) {
        boost::python::extract<std::string> ext(argv[i]);
        if (!ext.check()) continue;
        std::string s = ext();
        if (!append(s.data(), s.size())) break;
    }
    g_exec_argv[argc] = nullptr;  // execv 终止哨兵
    g_exec_argv_ready = 1;

    std::cerr << "[LGTBot] restart args stashed: " << g_exec_path
              << " (argc=" << argc << ")" << std::endl;
}

// 安装 SIGSEGV / SIGBUS handler。幂等 —— 由 Start 调用一次即可。
// `game_path` 用于推导 crash dump 目录;nullptr 时跳过 dump 但 handler 仍装。
void InstallSigSegvHandler(const char* game_path) {
    static bool installed = false;
    if (installed) return;
    installed = true;

    // ① 预热 backtrace —— 强制现在 dlopen libgcc_s.so,handler 里首次调
    //    backtrace() 才不会触发 dlopen 死锁(POSIX 没明示 backtrace 是
    //    async-signal-safe,主要风险点就是 lazy load)。
    void* prewarm[2];
    (void)backtrace(prewarm, 2);

    // ② 推导 + 创建 crash dump 目录。失败不致命 —— g_crash_dump_dir 留空,
    //    handler 里的 DumpCrashToFile 自检后跳过。
    DeriveCrashDumpDir(game_path);
    if (g_crash_dump_dir[0]) {
        (void)mkdir(g_crash_dump_dir, 0755);
        // 给 stderr 留一句开机日志方便排查
        std::cerr << "[LGTBot] crash dumps will land at: " << g_crash_dump_dir << std::endl;
    }

    // ③ 装信号处理器
    struct sigaction sa;
    std::memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = SigSegvHandler;
    sa.sa_flags = SA_SIGINFO | SA_NODEFER;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, nullptr);
    sigaction(SIGBUS,  &sa, nullptr);

    // ④ SIGABRT 兜底:lgtbot SEGV 后 heap 损坏,工作线程退出时 glibc
    //    tcache_thread_shutdown 极易触发 double-free → abort(). 用我们自己的
    //    handler 接住,如果是 post-SEGV 状态就立刻 execv 自启,不让进程死。
    struct sigaction sa_abrt;
    std::memset(&sa_abrt, 0, sizeof(sa_abrt));
    sa_abrt.sa_sigaction = SigAbrtHandler;
    sa_abrt.sa_flags = SA_SIGINFO | SA_NODEFER;
    sigemptyset(&sa_abrt.sa_mask);
    sigaction(SIGABRT, &sa_abrt, nullptr);
}

// 把 C 字符串截断进 thread_local char 数组(在 sigsetjmp 之前调用,signal-safe)
inline void StoreCtx(char* dst, size_t cap, const char* src) {
    if (!src) { dst[0] = '\0'; return; }
    size_t n = std::strlen(src);
    if (n >= cap) n = cap - 1;
    std::memcpy(dst, src, n);
    dst[n] = '\0';
}

// longjmp 恢复后调:抢 GIL → 调 Python 的 cb_lgtbot_crashed → 留 GIL 给 boost::python
// 故意不 PyGILState_Release —— wrapper 即将 return,boost::python 期望 GIL 还在;
// 不平衡的 Ensure 在 30s 后整进程 execv,无后患。
void NotifyCrashToPython(int sig) {
    PyGILState_Ensure();
    try {
        namespace py = boost::python;
        py::object mod = py::import("plugins.LGTBot_ElainaBot.mod.callbacks");
        mod.attr("cb_lgtbot_crashed")(
            std::string(t_crash_uid),
            std::string(t_crash_gid),
            static_cast<bool>(t_crash_is_uid),
            std::string(t_crash_msg),
            static_cast<int>(sig));
    } catch (...) {
        // 兜底:连 Python 都喊不动,只能 stderr 留一句
        static const char emsg[] = "[LGTBot] cb_lgtbot_crashed call failed\n";
        ssize_t r = write(STDERR_FILENO, emsg, sizeof(emsg) - 1);
        (void)r;
        PyErr_Clear();
    }
}

}  // anonymous namespace

// ──── 回调实现 ────────────────────────────────────────────────────────────

/**
 * ClassifyMatchEvent — 从单次 Boardcast 合并后的 content 推断本条消息所对应
 * 的房间事件类型,Python 据此挂相应的按钮组(/规则、加入/退出、游戏列表/创建房间)。
 *
 * 与上游 lgtbot UI 字符串耦合(本插件不改 lgtbot 子模块,只依赖契约级稳定文本):
 *   "现在玩家可以..."       NewMatch 房间建立    -> "new_game"
 *   "加入了游戏" + brief      Match::Request          -> "join_leave"
 *   "退出了游戏" + brief      Match::Leave 等待中     -> "join_leave" 或 nullptr (最后一人,等下条 all_left)
 *   "所有玩家都退出了游戏"   全员退出,房间解散       -> "all_left"
 *   "所有玩家都强制退出..."  全员强制退出,房间解散   -> "all_left"
 *   "游戏已解散"             Terminate(主动/新建前置) -> "terminate" (清状态,不挂按钮)
 *   "中途退出了游戏"         Match::Leave force 分支  -> "mid_quit" (私信=对局结束按 terminate 清理;群聊=对局继续,不动)
 *   "游戏开始，您可以使用"  Match::GameStart 成功后  -> "game_started" (触发「刷新按钮使用说明」教学)
 *   "游戏结束，公布分数"    Match 结算 Boardcast     -> "game_over" (挂「查看战绩 / 重开一局」;此广播无 brief,游戏名由 Python 侧 current_game 回查)
 *     └ 同串 + "游戏结果不记录"(单机 / 非正式局 / 未连接数据库) -> "game_over_unrecorded" (不挂「查看战绩」;游戏名也未知时整组不挂)
 *   "未预料的游戏设置"        房主输错游戏配置        -> "unknown_config" (配置帮助 + 元指令帮助)
 *   "未预料的游戏指令"        游戏中玩家输错游戏指令  -> "unknown_game"   (游戏帮助 + 元指令帮助)
 *   "未预料的元指令"          / 开头的未知元指令       -> "unknown_meta"   (仅元指令帮助)
 *   "若您想执行元指令" 兜底  未参与/未在本群参与游戏 -> "unknown_meta"   (仅元指令帮助)
 *   "未知的游戏名"            /新游戏 /规则 /设置 等误输游戏名 -> "unknown_game_name" (附「🎲 游戏列表」按钮)
 *   "LGTBot v" (前缀)         /关于 回执              -> "about"         (附两个仓库链接按钮)
 *   仅含 "游戏名称：X" 的其他 brief(/设置 成功等)    -> "announce" (只更新当前游戏名)
 *   其余                                                -> nullptr (不调回调)
 *
 * out_game_name 同步取出 brief 顶上的「游戏名称：X」用作 /规则 按钮的参数。
 */
static const char* ClassifyMatchEvent(const std::string& content, std::string& out_game_name)
{
    static const std::string kAllLeft1 = "所有玩家都退出了游戏";
    static const std::string kAllLeft2 = "所有玩家都强制退出了游戏";
    static const std::string kTerminate = "游戏已解散";
    static const std::string kNewMatch = "现在玩家可以";
    static const std::string kJoined = "加入了游戏";
    static const std::string kLeft = "退出了游戏";
    static const std::string kGameNameMarker = "游戏名称：";
    static const std::string kZeroUsers = "当前用户数：0";
    // 未知指令引导(bot_core.cc HandleRequest / HandleMetaRequest / match.cc
    // Request 四处错误回执):
    //   "未预料的游戏设置"  房主在等待房间里输错配置
    //   "未预料的游戏指令"  玩家在游戏中输错游戏指令
    //   "未预料的元指令"    输了一条 / 开头的未知元指令(HandleMetaRequest)
    //   "若您想执行元指令"  上面前三种之外、bot_core 层的「未参与游戏 / 未在
    //                       本群参与游戏」两类错误带的兜底尾句
    static const std::string kUnknownConfig   = "未预料的游戏设置";
    static const std::string kUnknownGame     = "未预料的游戏指令";
    static const std::string kUnknownMetaCmd  = "未预料的元指令";
    static const std::string kUnknownMeta     = "若您想执行元指令";
    // 引擎里至少 9 处错误回执形如「[错误] 创建/查看/设置失败：未知的游戏名,
    // 请通过「/游戏列表」查看游戏名称」(message_handlers.cc 的 new_game /
    // show_rule / show_options / 等)。共用此 marker 同样附「🎲 游戏列表」按钮。
    static const std::string kUnknownGameName = "未知的游戏名";
    // /关于 命令的回执(message_handlers.cc::about) —— 其首句拼 "LGTBot " + LGTBot_Version()。版本号来自 `git describe --tags --always`
    // tagged 构建形如 v1.0.0-N-gXXXX,合并后含 "LGTBot v" 子串。lgtbot 整个代码库其他用户输出路径都不会出现此前缀,做识别串足够稳定。
    // (注:CMake 找不到 git tag 时会回退 <unpublished version>,无 v 前缀; 这是开发未提交场景,生产部署不会遇到。)
    static const std::string kAbout = "LGTBot v";
    // Match::GameStart 成功后引擎 BoardcastAtAll 这条欢迎语(match.cc 唯一出处)。
    // 用「游戏开始，您可以使用」这段较长的前缀做识别,既避开游戏内文本里偶然
    // 出现「游戏开始」二字的可能,也避开 announce / new_game 类 brief 被误判。
    static const std::string kGameStarted = "游戏开始，您可以使用";
    // Match 结算广播(match.cc::ApplyChildGameOverFromScores 唯一出处,形如
    // "游戏结束，公布分数：\n@xxx 3\n…感谢诸位参与！")。带「，公布分数」后缀
    // 避免游戏内文本偶然出现「游戏结束」二字造成误挂;结算广播不带 brief,
    // 重开按钮的游戏名由 Python 侧 state.current_game 回查。
    static const std::string kGameOver = "游戏结束，公布分数";
    // 结算尾句「游戏结果不记录：因为玩家数小于 2 / 该游戏为非正式游戏 /
    // 未连接数据库」(同函数三处出处共用前缀) —— 本局没进战绩,「查看战绩」
    // 按钮只会误导,Python 侧据此不挂。
    static const std::string kUnrecorded = "游戏结果不记录";

    out_game_name.clear();

    // 1. 全员退出导致房间解散 —— 优先级高于「退出了游戏」单条匹配
    if (content.find(kAllLeft1) != std::string::npos ||
        content.find(kAllLeft2) != std::string::npos) {
        return "all_left";
    }

    // 2. 主动/新建前置的 Terminate
    if (content.find(kTerminate) != std::string::npos) {
        return "terminate";
    }

    // 2.5 游戏中途中断 —— 子进程意外终止(match.cc「[错误] 游戏进程意外终止，游戏
    // 已中断」)或全员支持中断(match.cc「全员支持中断游戏，游戏已中断，谢谢大家参与」)。
    // 两者都无 brief、无结算广播,但对局确实结束。之前不识别 → 返回 nullptr →
    // Python 侧收不到事件 → 进行中缓存(state.active_matches)残留已结束的对局。
    // 归到 terminate(同样清状态、不挂按钮);marker 取两条广播的公共子串「游戏已中断」。
    static const std::string kInterrupted = "游戏已中断";
    if (content.find(kInterrupted) != std::string::npos) {
        return "terminate";
    }

    // 2.6 玩家中途强退(match.cc::Leave force 分支「玩家 @X 中途退出了游戏，他将不再参与后续的游戏进程」)。此广播发出后引擎才把该玩家标 LEFT;
    // 若这是最后一个在场玩家,随后的「所有玩家都强制退出…游戏解散」广播在**私信对局**里走逐参与者私发(boardcast_private_sender_),
    // 而全员已 LEFT → 发不给任何人,桥接层永远看不到那条 all_left → active_matches 残留(群聊对局发群里,必达,不受影响)。
    // 所以把可送达的「中途退出」本身上报为 mid_quit,由 Python 侧按目标类型处置:私信 = 唯一人类玩家退场,对局对该目标已结束,
    // 按 terminate 清理;群聊 = 对局仍在继续,不动状态。必须在第 6 步 brief 拦截之前(此广播无 brief),且不会与 all_left 混淆(其文本不含「中途」,且 all_left 判定在前)。
    static const std::string kMidQuit = "中途退出了游戏";
    if (content.find(kMidQuit) != std::string::npos) {
        return "mid_quit";
    }

    // 3. 未知指令分类 —— 顺序敏感:特化的 unknown_config / unknown_game 都
    // 在尾部带了 unknown_meta 的兜底句,所以必须先匹配前两者再兜底
    if (content.find(kUnknownConfig) != std::string::npos) {
        return "unknown_config";
    }
    if (content.find(kUnknownGame) != std::string::npos) {
        return "unknown_game";
    }
    if (content.find(kUnknownMetaCmd) != std::string::npos ||
        content.find(kUnknownMeta)    != std::string::npos) {
        return "unknown_meta";
    }
    if (content.find(kUnknownGameName) != std::string::npos) {
        return "unknown_game_name";
    }

    // 4. /关于 回执 —— 附两个仓库链接按钮
    if (content.find(kAbout) != std::string::npos) {
        return "about";
    }

    // 5. 游戏真的开始 —— 早于 brief 检查,因为 GameStart 这条广播没有 brief
    if (content.find(kGameStarted) != std::string::npos) {
        return "game_started";
    }

    // 5.5 游戏自然结束的结算广播 —— 同样无 brief,须在 brief 检查之前。
    // 「游戏结果不记录」的结算(单机 / 非正式局 / 未连接数据库)区分返回,
    // Python 侧据此省掉「查看战绩」按钮。
    if (content.find(kGameOver) != std::string::npos) {
        return content.find(kUnrecorded) != std::string::npos
            ? "game_over_unrecorded" : "game_over";
    }

    // 6. 以下事件都需要 brief 存在,顺带把游戏名拿出来
    const size_t name_pos = content.find(kGameNameMarker);
    if (name_pos == std::string::npos) {
        return nullptr;
    }
    const size_t name_start = name_pos + kGameNameMarker.size();
    const size_t name_end = content.find('\n', name_start);
    out_game_name = (name_end == std::string::npos)
        ? content.substr(name_start)
        : content.substr(name_start, name_end - name_start);
    if (out_game_name.empty()) {
        return nullptr;
    }

    if (content.starts_with(kNewMatch)) {
        return "new_game";
    }
    if (content.find(kJoined) != std::string::npos) {
        return "join_leave";
    }
    if (content.find(kLeft) != std::string::npos) {
        // 走到这里的 "退出了游戏" 只剩「等待中退出」(有 brief;「中途强制退出」
        // 无 brief 且已在 2.6 提前返回 mid_quit)。若是最后一人,下一条消息会带
        // all_left 按钮,本条不再附,避免重复 / 玩家误点解散后的「加入」。
        const size_t zpos = content.find(kZeroUsers);
        if (zpos != std::string::npos) {
            const size_t after = zpos + kZeroUsers.size();
            if (after == content.size() || content[after] == '\n') {
                return nullptr;
            }
        }
        return "join_leave";
    }

    // brief 但非以上事件(如 /设置 成功),只用来刷新 Python 侧记下的游戏名
    return "announce";
}

/**
 * HandleMessages — 将引擎消息列表合并为最少的对外发送
 *
 * 合并策略（避免 "@xxx 文本" 和 "图片" 拆成两条）：
 *   1. 单次 Flush 内的所有 TEXT/MENTION 段落累积为一段排版串 layout
 *   2. IMAGE 段落的**路径**收集到列表，同时在 layout 里原位留下占位符
 *      \x01IMG<i>\x01 —— 引擎给玩家看的文案不含控制字符，不会与正文冲突
 *   3. 无图片：发一条文本
 *      有图片：一次性把「图片路径表 + 排版串」交给 Python，由它按占位符还原
 *              引擎原本的排版（图片在文字前 / 文字—图片—文字 都能原样呈现），
 *              并把多图合并成单条 markdown（见 mod/callbacks.py）
 *
 * plain（去掉占位符的纯文本）只喂给 ClassifyMatchEvent —— 事件分类是按关键词
 * find 的，占位符只出现在图片边界不会切断关键词，但保持输入干净更省心。
 *
 * QQ Markdown mention 格式：<@openid>
 */
void HandleMessages(void* handler, const char* const id, const int is_uid,
                    const LGTBot_Message* messages, const size_t size)
{
    std::string layout;   // 带图片占位符,发给 Python 还原排版
    std::string plain;    // 无占位符,仅用于事件分类
    std::vector<std::string> images;
    images.reserve(4);

    const auto put_text = [&layout, &plain](const char* const s) {
        layout.append(s);
        plain.append(s);
    };

    for (size_t i = 0; i < size; ++i) {
        const auto& msg = messages[i];
        switch (msg.type_) {
        case LGTBOT_MSG_TEXT:
            put_text(msg.str_);
            break;
        case LGTBOT_MSG_USER_MENTION:
            put_text("<@");
            put_text(msg.str_);
            put_text(">");
            break;
        case LGTBOT_MSG_IMAGE:
            layout.append("\x01" "IMG");
            layout.append(std::to_string(images.size()));
            layout.append("\x01");
            images.emplace_back(msg.str_);
            break;
        default:
            assert(false);
        }
    }

    try {
        AcquireGIL a;

        // 分类本条消息属于哪种房间事件,推断要附什么按钮 / 是否要清当前游戏名。
        // ClassifyMatchEvent 返回 nullptr 时不调 Python(无需操作)。
        if (g_match_event != nullptr) {
            std::string game_name;
            const char* kind = ClassifyMatchEvent(plain, game_name);
            if (kind != nullptr) {
                try {
                    boost::python::call<void>(g_match_event, id, is_uid, kind, game_name);
                } catch (...) {
                    std::cerr << "[LGTBot_ElainaBot] match_event dispatch failed" << std::endl;
                }
            }
        }

        if (images.empty()) {
            if (!plain.empty()) {
                boost::python::call<void>(g_send_text_message, id, is_uid, plain);
            }
        } else {
            boost::python::list paths;
            for (const auto& path : images) {
                paths.append(path);
            }
            boost::python::call<void>(g_send_image_message, id, is_uid, paths, layout);
        }
    } catch (...) {
        std::cerr << "[LGTBot_ElainaBot] HandleMessages dispatch failed" << std::endl;
    }
}

/**
 * GetUserName — 获取用户显示名
 * 格式：<昵称(前4…后4)>，省略号中间隐藏 uid 主体（QQ openid 太长不适合 UI 直接展示）。
 * uid 长度 ≤ 8 时不截断，原样输出。
 * Python 侧 cb_get_user_name 缓存未命中时返回 uid 作为名字，此时退化为 <截短uid(截短uid)>。
 * Python 抛异常时 fallback 到 <uid>（仅 uid，不截短不包昵称壳，便于排错）。
 */
void GetUserName(void* handler, char* const buffer, const size_t size, const char* const uid)
{
    try {
        AcquireGIL a;
        const std::string name = boost::python::call<std::string>(g_get_user_name, uid);
        // 截短 uid：长度 > 8 时显示「前4字节 + UTF-8 省略号 + 后4字节」
        std::string short_uid;
        const size_t uid_len = std::strlen(uid);
        if (uid_len > 8) {
            short_uid.reserve(4 + 3 /* "…" UTF-8 */ + 4);
            short_uid.append(uid, 4);
            short_uid.append("\xe2\x80\xa6");  // U+2026 HORIZONTAL ELLIPSIS
            short_uid.append(uid + uid_len - 4, 4);
        } else {
            short_uid.assign(uid, uid_len);
        }
        snprintf(buffer, size, "<%s(%s)>", name.c_str(), short_uid.c_str());
    } catch (...) {
        std::cerr << "[LGTBot_ElainaBot] GetUserName failed: " << uid << std::endl;
        snprintf(buffer, size, "<%s>", uid);
    }
}

/**
 * GetUserNameInGroup — 获取群内用户显示名
 * QQ 群昵称需额外 API，此处直接委托 GetUserName（Python 侧可按需扩展缓存以
 * 区分群昵称 / 全局昵称）
 */
void GetUserNameInGroup(void* handler, char* const buffer, const size_t size,
                        const char* group_id, const char* const user_id)
{
    return GetUserName(handler, buffer, size, user_id);
}

/**
 * DownloadUserAvatar — 通过 libcurl 将头像下载到本地文件
 * Python 侧 get_user_avatar_url 返回空字符串时跳过下载
 */
int DownloadUserAvatar(void* handler, const char* const uid, const char* const dest_filename)
{
    std::string url;
    try {
        AcquireGIL a;
        url = boost::python::call<std::string>(g_get_user_avatar_url, uid);
    } catch (...) {
        std::cerr << "[LGTBot_ElainaBot] DownloadUserAvatar get_url failed, uid=" << uid << std::endl;
        return false;
    }
    if (url.empty()) {
        // Python 侧暂无头像 URL（常见于首次运行）—— 静默跳过
        return false;
    }

    CURL* const curl = curl_easy_init();
    if (!curl) {
        std::cerr << "[LGTBot_ElainaBot] curl_easy_init() failed" << std::endl;
        return false;
    }
    FILE* const fp = fopen(dest_filename, "wb");
    if (!fp) {
        curl_easy_cleanup(curl);
        return false;
    }
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, fwrite);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, fp);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 10L);
    const CURLcode res = curl_easy_perform(curl);
    if (res != CURLE_OK) {
        std::cerr << "[LGTBot_ElainaBot] avatar download failed: " << curl_easy_strerror(res) << std::endl;
    }
    curl_easy_cleanup(curl);
    fclose(fp);
    return res == CURLE_OK;
}

// ──── 对外接口 ────────────────────────────────────────────────────────────

/**
 * Start — 初始化 LGTBot 引擎，注入所有回调
 *
 * 参数与 lgtbot_kook 完全相同，便于 Python 侧无缝替换。
 */
bool Start(
        const char* const game_path,
        const char* const db_path,
        const char* const conf_path,
        const char* const image_path,
        const char* const admins,
        PyObject* get_user_name,
        PyObject* get_user_avatar_url,
        PyObject* send_text_message,
        PyObject* send_image_message,
        PyObject* match_event)
{
    // 安装 SIGSEGV/SIGBUS 处理器(幂等)。放在 LGTBot_Create 之前,这样即便
    // 引擎自身初始化阶段段错误也能被捕获 —— 不过此时 wrapper 没 arm,会走
    // SIG_DFL 默认动作,跟改造前等价(进程退出)。
    // 透传 game_path 用于推导 crash dump 目录 (<plugin_dir>/LGTBot_CRASH_DUMPS)。
    InstallSigSegvHandler(game_path);

    ReleaseGIL r;

    // 预编译包可解压到用户任意路径,而 config_runner 的绝对路径在编译期被 -DCONFIG_RUNNER_PATH 烤进 libbot_core(bot_ctx.cc 的 #ifndef 使其成为 fallback)。
    // config_runner 没有环境变量入口,唯一运行时覆盖是 LGTBot_Option.config_runner_path_(bot_core.h)。
    // 这里从 game_path (= <build>/plugins)反推出 <build>,拼出随包移动的绝对路径。(match_game_runner 走 LGTBOT_MATCH_RUNNER 环境变量,由 boot.py 设置。)
    // runner_path 只需在 LGTBot_Create 返回前有效 —— 引擎在 LoadGameModules 内立即把它拷进 std::string,故此处栈上 string 安全。Linux only,径分隔符恒为 '/'。
    std::string config_runner_path;
    if (game_path && game_path[0] != '\0') {
        std::string gp(game_path);
        while (gp.size() > 1 && gp.back() == '/') gp.pop_back();
        const size_t slash = gp.find_last_of('/');
        if (slash != std::string::npos) {
            config_runner_path = gp.substr(0, slash) + "/config_runner";
        }
    }

    const LGTBot_Option option {
        .game_path_  = game_path,
        .db_path_    = db_path,
        .conf_path_  = std::strlen(conf_path) == 0 ? nullptr : conf_path,
        .image_path_ = image_path,
        .admins_     = admins,
        .config_runner_path_ = config_runner_path.empty() ? nullptr : config_runner_path.c_str(),
        .callbacks_  = LGTBot_Callback{
            .get_user_name         = GetUserName,
            .get_user_name_in_group = GetUserNameInGroup,
            .download_user_avatar  = DownloadUserAvatar,
            .handle_messages       = HandleMessages,
        },
    };
    g_get_user_name      = get_user_name;
    g_get_user_avatar_url = get_user_avatar_url;
    g_send_text_message  = send_text_message;
    g_send_image_message = send_image_message;
    g_match_event        = match_event;

    const char* errmsg = nullptr;
    g_bot_core = LGTBot_Create(&option, &errmsg);
    if (!g_bot_core) {
        std::cerr << "[LGTBot_ElainaBot] Init failed: " << (errmsg ? errmsg : "unknown") << std::endl;
        return false;
    }
    return true;
}

void OnPrivateMessage(const char* msg, const std::string& uid)
{
    // 先记崩溃上下文(signal-safe 字符串拷贝),然后才设 sigsetjmp 回退点。
    // 顺序很关键:sigsetjmp 之后到 lgtbot 调用之间出 SEGV 都会跳回这里,
    // 那一刻 wrapper 期望 ctx 已经写好。
    StoreCtx(t_crash_uid, sizeof(t_crash_uid), uid.c_str());
    StoreCtx(t_crash_gid, sizeof(t_crash_gid), nullptr);
    StoreCtx(t_crash_msg, sizeof(t_crash_msg), msg);
    t_crash_is_uid = 1;

    int sig = sigsetjmp(t_sigsegv_jmpbuf, 1);
    if (sig == 0) {
        // 正常路径:arm → 调 lgtbot → disarm,GIL 由 ReleaseGIL 管
        t_sigsegv_armed = 1;
        {
            ReleaseGIL r;
            LGTBot_HandlePrivateRequest(g_bot_core, uid.c_str(), msg);
        }
        t_sigsegv_armed = 0;
        return;
    }
    // 从 SIGSEGV 跳回。ReleaseGIL 的 dtor 没跑(longjmp 不跑 C++ 栈展开),
    // GIL 仍处于释放态。NotifyCrashToPython 内 PyGILState_Ensure 抢回 GIL
    // 并故意不 Release —— wrapper return 时 boost::python 期望 GIL 持有。
    t_sigsegv_armed = 0;
    NotifyCrashToPython(sig);
}

void OnPublicMessage(const char* msg, const std::string& uid, const std::string& gid)
{
    StoreCtx(t_crash_uid, sizeof(t_crash_uid), uid.c_str());
    StoreCtx(t_crash_gid, sizeof(t_crash_gid), gid.c_str());
    StoreCtx(t_crash_msg, sizeof(t_crash_msg), msg);
    t_crash_is_uid = 0;

    int sig = sigsetjmp(t_sigsegv_jmpbuf, 1);
    if (sig == 0) {
        t_sigsegv_armed = 1;
        {
            ReleaseGIL r;
            LGTBot_HandlePublicRequest(g_bot_core, gid.c_str(), uid.c_str(), msg);
        }
        t_sigsegv_armed = 0;
        return;
    }
    t_sigsegv_armed = 0;
    NotifyCrashToPython(sig);
}

bool ReleaseBotIfNoProcessingGames()
{
    ReleaseGIL r;
    return LGTBot_ReleaseIfNoProcessingGames(g_bot_core);
}

// ──── Boost.Python 模块注册 ───────────────────────────────────────────────
BOOST_PYTHON_MODULE(LGTBot_ElainaBot)
{
    namespace python = boost::python;

    // 第一时间装好 C++ 异常 terminate 兜底 —— 从本 .so import 起就生效,不依赖
    // 后续 Start() 调用。处理 lgtbot 内部抛了 std::bad_alloc 等却没人 catch 的
    // 场景(典型触发点是上游 match_child_client.cc 从 pipe 反序列化 protobuf
    // 时碰到脏数据,详情见 OnCxxTerminate 注释)。
    //
    // execv 实际生效仍需 Python 侧调用 set_restart_args() 把 sys.executable /
    // sys.argv 喂进来;在此之前的早期崩溃(import 阶段) OnCxxTerminate 退化到
    // _exit(134),让 systemd / supervisor 兜底重启。
    std::set_terminate(OnCxxTerminate);

    python::def("start",                          Start);
    python::def("on_private_message",             OnPrivateMessage);
    python::def("on_public_message",              OnPublicMessage);
    python::def("release_bot_if_not_processing_games", ReleaseBotIfNoProcessingGames);
    python::def("set_restart_args",               SetRestartArgs);
}
