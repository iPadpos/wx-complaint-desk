window.addEventListener('load', () => {
  requestAnimationFrame(() => {
    document.querySelectorAll('.selector .helptext').forEach(text => text.remove());
  });
});
