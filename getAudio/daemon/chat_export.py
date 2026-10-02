from datetime import datetime
import sys
import os
import re
import subprocess
import os



curr_date = datetime.now().strftime("%Y_%m_%d_%H_%M_%s")

def c_time():
    return datetime.now().strftime("%Y_%m_%d_%H_%M_%s")


os.makedirs("chat_history", exist_ok=True)


def save_chat(messages): #对聊天记录做导出
    temp = ""
    for i in range(len(messages)):
        if(messages[i]["role"]!="system"):
            time = messages[i].get("time","")
            temp += "## "
            temp += (messages[i]["role"])
            temp += "  "
            temp += " (" + time +" )"
            temp += ":\n"
            temp += messages[i]["content"]
            temp += "\n"
            temp += "---"
            temp += "\n"
    with open("chat_history/agent_chat_history"+curr_date+".md","w") as f:
        f.write(temp)
