'use strict';
const form = document.querySelector('#panel-login-form');
const password = document.querySelector('#panel-password');
const button = document.querySelector('#panel-login-submit');
const error = document.querySelector('#panel-login-error');
document.querySelector('#show-password').addEventListener('click', event => {
  const show = password.type === 'password';
  password.type = show ? 'text' : 'password';
  event.currentTarget.textContent = show ? '隐藏' : '显示';
  event.currentTarget.setAttribute('aria-label', show ? '隐藏密码' : '显示密码');
  event.currentTarget.setAttribute('aria-pressed', String(show));
});
form.addEventListener('submit', async event => {
  event.preventDefault();
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = '登录中…';
  error.hidden = true;
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content},
      body: JSON.stringify({password: password.value})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '登录失败，请重试。');
    password.value = '';
    location.reload();
  } catch (failure) {
    error.textContent = failure instanceof TypeError ? '无法连接服务，请稍后重试。' : failure.message;
    error.hidden = false;
    password.focus();
  } finally {
    button.disabled = false;
    button.textContent = '登录';
  }
});
