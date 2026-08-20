"""生成一段极短的静音 wav，供 CI 里给打包产物做「上传→ffmpeg解码→本地转写」
链路的冒烟测试用。单独写成文件而不是内嵌在 workflow YAML 里的 heredoc——
heredoc 正文顶格写会破坏 YAML block scalar 的缩进约定，缩进了又会破坏
Python 自己的语法，两头不讨好，不如干脆拆成一个可以本地单独跑测的脚本。
"""
import struct
import sys
import wave


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "smoke_test.wav"
    with wave.open(out_path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<8000h", *([0] * 8000)))  # 0.5s 静音


if __name__ == "__main__":
    main()
