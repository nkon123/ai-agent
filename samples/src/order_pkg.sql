-- 주문 패키지. TOTAL_AMT 를 갱신한다(주석이므로 사용처가 아니다)
CREATE OR REPLACE PROCEDURE UPD_ORDER(p_ord_no IN VARCHAR2) AS
  v_msg VARCHAR2(200) := 'TOTAL_AMT is not a hit -- inside string';
BEGIN
  UPDATE ORDERS SET TOTAL_AMT = TOTAL_AMT + 1 WHERE ORD_NO = p_ord_no;
  /* IF_A 는 여기서만 쓰인다 */
  NULL;
END;
