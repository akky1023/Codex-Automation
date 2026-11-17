<?php
// Run only inside the local Nextcloud test container after live-spinner.cjs.
define('OC_CONSOLE', true);
require '/var/www/html/lib/base.php';
$owner = $argv[1] ?? '';
if (!str_starts_with($owner, 'cq-spinner-')) {
    throw new \InvalidArgumentException('A dedicated cq-spinner-* test account is required.');
}
$db = \OCP\Server::get(\OCP\IDBConnection::class);
foreach (['cq_requests', 'cq_boards'] as $table) {
    $query = $db->getQueryBuilder();
    $query->delete($table)
        ->where($query->expr()->eq('owner', $query->createNamedParameter($owner)))
        ->executeStatement();
}
echo "Isolated spinner test board cleared\n";
