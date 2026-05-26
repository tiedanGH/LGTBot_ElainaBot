cmake_minimum_required(VERSION 3.15)
project(lgtbot-qq LANGUAGES CXX C)

# ── C++ 标准 ────────────────────────────────────────────────────────────────
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ── MSVC UTF-8 ───────────────────────────────────────────────────────────────
if(MSVC)
    add_compile_options("$<$<C_COMPILER_ID:MSVC>:/utf-8>")
    add_compile_options("$<$<CXX_COMPILER_ID:MSVC>:/utf-8>")
endif()

# ── 元指令前缀:锁定为 "/" ────────────────────────────────────────────────
add_compile_definitions(META_COMMAND_SIGN="/")

# ── LGTBot C++ 核心（子目录/子模块）────────────────────────────────────────
add_subdirectory(lgtbot)

# ── 依赖查找 ─────────────────────────────────────────────────────────────────
find_package(CURL REQUIRED)

# Python 3：优先匹配当前运行的解释器
find_package(Python3 REQUIRED COMPONENTS Interpreter Development)
message(STATUS "Python3: ${Python3_VERSION}  include=${Python3_INCLUDE_DIRS}")

# Boost.Python：按版本号尝试多种命名约定
#   libboost-python3.10-dev → boost_python310
#   libboost-python3-dev    → boost_python3
#   libboost-python-dev     → boost_python
set(_BP_FOUND FALSE)
foreach(_suffix
        "${Python3_VERSION_MAJOR}${Python3_VERSION_MINOR}"
        "${Python3_VERSION_MAJOR}"
        "")
    find_package(Boost QUIET COMPONENTS "python${_suffix}")
    if(Boost_FOUND)
        message(STATUS "Boost.Python component: python${_suffix}  libs=${Boost_LIBRARIES}")
        set(_BP_FOUND TRUE)
        break()
    endif()
endforeach()
if(NOT _BP_FOUND)
    message(FATAL_ERROR
        "找不到 Boost.Python。\n"
        "Ubuntu/Debian: sudo apt install libboost-python3-dev\n"
        "或手动指定: cmake -DBoost_INCLUDE_DIR=... -DBoost_LIBRARY_DIR=...")
endif()

# ── 编译目标 ──────────────────────────────────────────────────────────────────
add_library(LGTBot_ElainaBot SHARED LGTBot_ElainaBot.cc)

# 不加 lib 前缀；编译产物直接输出到插件源目录，Python import 时可直接找到
set_target_properties(LGTBot_ElainaBot PROPERTIES
    PREFIX ""
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"   # Windows .dll
)

target_include_directories(LGTBot_ElainaBot PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/lgtbot"
    ${Python3_INCLUDE_DIRS}
    ${Boost_INCLUDE_DIRS}
)

target_link_libraries(LGTBot_ElainaBot PRIVATE
    bot_core
    ${Python3_LIBRARIES}
    ${Boost_LIBRARIES}
    ${CURL_LIBRARIES}
)

# gflags（LGTBot 内部使用，可选）
find_package(gflags QUIET)
if(gflags_FOUND)
    target_link_libraries(LGTBot_ElainaBot PRIVATE gflags)
else()
    # 尝试直接链接，部分系统没有 find_package 但库存在
    target_link_libraries(LGTBot_ElainaBot PRIVATE -lgflags)
endif()

# pthread（Linux GCC/Clang）
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_link_libraries(LGTBot_ElainaBot PRIVATE pthread)
endif()

add_dependencies(LGTBot_ElainaBot bot_core)

message(STATUS "编译完成后 LGTBot_ElainaBot.so 将输出至: ${CMAKE_CURRENT_SOURCE_DIR}")
