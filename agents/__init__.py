"""에이전트 모음.

각 에이전트는 agents/<name>/agent.py 에 다음을 갖춘다.
    - LangGraph 흐름 (노드 = 판정 단계)
    - run_<name>(..., detail="full") -> dict   진입점
    - if __name__ == "__main__" CLI

챗봇에 붙이는 얇은 껍데기는 app/tools/<name>.py 에 따로 둔다.
에이전트는 app/ 을 import 하지 않는다(단독 실행이 가능해야 한다).
"""
