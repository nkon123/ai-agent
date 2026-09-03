/* 주문 금액 계산 — TOTAL_AMT 를 여기서 채운다는 설명(주석이므로 사용처가 아니다) */
#include <stdio.h>

int IF_A;          /* TOTAL_AMT 와 무관한 별개 식별자 */
long TOTAL_AMT;

long calc_total(long unit, long qty)
{
    long r = unit*/*단가 보정*/qty;   /* 포인터 연산과 주석이 붙어 있는 형태 */
    TOTAL_AMT = r;
    return TOTAL_AMT;
}

void log_fmt(void)
{
    char *s = "/* TOTAL_AMT */";      /* 문자열 안의 주석 토큰 */
    printf("%s\n", s);
}
