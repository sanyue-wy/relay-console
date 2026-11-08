/* 一次性 token 换 cookie：用 POST 换取，避免浏览器预取 GET 时提前消耗 token。 */
const token = new URLSearchParams(location.search).get('token') || '';
const xhr = new XMLHttpRequest();
xhr.open('POST', '/auth/exchange');
xhr.setRequestHeader('Content-Type', 'application/json');
xhr.onload = function () {
  if (xhr.status >= 200 && xhr.status < 300) {
    location.replace('/');
    return;
  }
  let msg = '登录链接已失效';
  try {
    msg = JSON.parse(xhr.responseText).detail || msg;
  } catch (e) { /* 非 JSON 响应 */ }
  const el = document.getElementById('msg');
  if (el) el.textContent = msg + '（可点右下角托盘图标重新生成）';
};
xhr.onerror = function () {
  const el = document.getElementById('msg');
  if (el) el.textContent = '无法连接控制台，请确认服务仍在运行';
};
xhr.send(JSON.stringify({ token: token }));