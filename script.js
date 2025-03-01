function showTab(tabId) {
    // Скрываем все вкладки
    const tabs = document.querySelectorAll('.tab-content');
    tabs.forEach(tab => {
        tab.classList.remove('active');
    });
    // Показываем выбранную вкладку
    document.getElementById(tabId).classList.add('active');
}

// Обновляем страницу каждые 60 секунд
setInterval(function() {
    location.reload();
}, 60000);
