<?php

declare(strict_types=1);

namespace OCA\CodexQueue\Migration;

use Closure;
use OCP\DB\ISchemaWrapper;
use OCP\DB\Types;
use OCP\Migration\IOutput;
use OCP\Migration\SimpleMigrationStep;

final class Version000200Date20261114000000 extends SimpleMigrationStep {
    public function changeSchema(IOutput $output, Closure $schemaClosure, array $options): ?ISchemaWrapper {
        $schema = $schemaClosure();
        $board = $schema->createTable('cq_boards');
        $board->addColumn('owner', Types::STRING, ['length' => 64, 'notnull' => true]);
        $board->addColumn('snapshot', Types::TEXT, ['length' => 16777215, 'notnull' => true]);
        $board->setPrimaryKey(['owner']);

        $requests = $schema->createTable('cq_requests');
        $requests->addColumn('id', Types::BIGINT, ['autoincrement' => true, 'notnull' => true]);
        $requests->addColumn('owner', Types::STRING, ['length' => 64, 'notnull' => true]);
        $requests->addColumn('kind', Types::STRING, ['length' => 16, 'notnull' => true]);
        $requests->addColumn('target_id', Types::BIGINT, ['notnull' => true]);
        $requests->addColumn('title', Types::STRING, ['length' => 200, 'notnull' => true]);
        $requests->addColumn('body', Types::TEXT, ['notnull' => true]);
        $requests->addColumn('outcome', Types::TEXT, ['notnull' => false]);
        $requests->setPrimaryKey(['id']);
        $requests->addIndex(['owner', 'id'], 'cq_requests_owner');
        return $schema;
    }
}
